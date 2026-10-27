"""FastAPI application: operator API, simulator controls, and live update stream."""

import asyncio
import base64
import hashlib
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .catalog import ASSETS, Fleet, site_context
from .config import BACKEND_DIR, Settings, load_settings
from .engine import Engine, EngineError
from .metrics import compute_metrics
from .plan import PlanValidationError
from .planner import FixturePlanner, LLMPlanner, Planner
from .simulator import SimulatedFleet
from .store import Store

# The optional demo password does not authenticate an individual operator.
# Actor names are supplied by the UI and recorded for audit only.
Actor = Field(default="operator", min_length=1, max_length=64)


class CreateMission(BaseModel):
    work_order: str = Field(min_length=5, max_length=4000)
    # Explicit intake fields: every plan for the mission is checked against them.
    target: str
    allow_repair: bool
    planner: str = "llm"
    actor: str = Actor


class EditPlan(BaseModel):
    plan: Any
    actor: str = Actor


class ActorOnly(BaseModel):
    actor: str = Actor


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    actor: str = Actor
    comment: str = Field(default="", max_length=1000)


class Fault(BaseModel):
    fault: Literal[
        "offline", "online", "lose_silently", "fail_next", "hang_next", "duplicate_next", "reorder_next", "clear"
    ]
    actor: str = Actor


class AssetScenario(BaseModel):
    defect_present: bool


def sim_state_file(database_path: str) -> Path | None:
    """Simulated asset state is saved next to the database, so both survive a restart together."""
    return None if database_path == ":memory:" else Path(database_path).with_suffix(".sim.json")


def build_planners(settings: Settings, fleet: Fleet) -> dict[str, Planner]:
    planners: dict[str, Planner] = {"fixture": FixturePlanner(fleet)}
    if settings.llm_configured:
        planners["llm"] = LLMPlanner(
            fleet=fleet,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            reasoning_effort=settings.llm_reasoning_effort or None,
            timeout_s=settings.llm_timeout_s,
        )
    return planners


def create_app(settings: Settings | None = None, planners: dict[str, Planner] | None = None) -> FastAPI:
    settings = settings or load_settings()
    # Fails at startup with every problem listed if the fleet file is invalid.
    fleet = Fleet.load(settings.fleet_file)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state_file = sim_state_file(settings.database_path)
        if state_file:
            Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
            if not Path(settings.database_path).exists():
                # A new database starts a new simulated world: deleting the database resets everything.
                state_file.unlink(missing_ok=True)
        store = Store(settings.database_path)
        sim = SimulatedFleet(fleet, time_scale=settings.sim_time_scale, state_file=state_file)
        engine = Engine(
            store,
            sim,
            fleet,
            planners or build_planners(settings, fleet),
            time_scale=settings.sim_time_scale,
            heartbeat_lease_s=settings.heartbeat_lease_s,
        )
        sim.attach(engine.enqueue, heartbeat=engine.heartbeat)
        await engine.start()
        sim.start()
        app.state.store, app.state.sim, app.state.engine = store, sim, engine
        try:
            yield
        finally:
            await engine.stop()
            await sim.shutdown()
            store.close()

    app = FastAPI(title="IAC Mission Orchestrator", lifespan=lifespan)

    demo_password = os.environ.get("DEMO_PASSWORD", "")
    if demo_password:
        expected = "Basic " + base64.b64encode(f"reviewer:{demo_password}".encode()).decode()

        @app.middleware("http")
        async def demo_access(request: Request, call_next):
            if request.url.path != "/health" and not secrets.compare_digest(
                request.headers.get("authorization", ""), expected
            ):
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="IAC Demo"'})
            return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(EngineError)
    async def engine_error(_: Request, exc: EngineError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(PlanValidationError)
    async def plan_error(_: Request, exc: PlanValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "plan failed validation", "errors": exc.errors})

    def engine_of(request: Request) -> Engine:
        return request.app.state.engine

    @app.get("/api/config")
    async def config(request: Request) -> dict:
        engine = engine_of(request)
        return {
            "planners": [{"mode": p.mode, "label": p.label} for p in engine.planners.values()],
            "default_planner": "llm" if "llm" in engine.planners else "fixture",
            "sim_time_scale": settings.sim_time_scale,
        }

    @app.get("/api/site")
    async def site() -> dict:
        return site_context(fleet)

    @app.get("/api/robots")
    async def robots(request: Request) -> dict:
        return robots_response(request)

    def robots_response(request: Request) -> dict:
        robots = engine_of(request).robots_view()
        types = []
        for t in fleet.types.values():
            members = [r for r in robots if r["type"] == t.id]
            counts: dict[str, int] = {}
            for r in members:
                counts[r["availability"]] = counts.get(r["availability"], 0) + 1
            types.append(
                {"id": t.id, "name": t.name, "capabilities": sorted(t.capabilities), "total": len(members), "availability": counts}
            )
        return {"types": types, "robots": robots, "simulator": request.app.state.sim.snapshot()}

    @app.get("/api/fleet-events")
    async def fleet_events(request: Request) -> list[dict]:
        return request.app.state.store.events(kinds=("robot_status", "unknown_command_event"))[-100:]

    @app.get("/api/missions")
    async def list_missions(request: Request) -> list[dict]:
        return request.app.state.store.missions()

    async def idempotent(request: Request, body: BaseModel, run: Callable[[], Awaitable[Any]], code: int = 200):
        """Run an operator command at most once per Idempotency-Key header.

        Without the header the command simply runs. With it, a repeat returns the stored response
        (including errors), a different body under the same key is rejected, and a concurrent
        duplicate is told to retry.
        """
        key = request.headers.get("Idempotency-Key")
        if key is None:
            return JSONResponse(status_code=code, content=await run())
        if not 1 <= len(key) <= 200:
            raise HTTPException(400, "Idempotency-Key must be 1 to 200 characters")
        store: Store = request.app.state.store
        scope = f"{request.method} {request.url.path}"
        request_hash = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
        state, row = store.claim_idempotency_key(scope, key, request_hash)
        if state == "mismatch":
            raise HTTPException(422, "Idempotency-Key was already used with a different request body")
        if state == "in_progress":
            raise HTTPException(409, "a request with this Idempotency-Key is still being processed")
        if state == "replay":
            return JSONResponse(
                status_code=row["response_code"], content=row["response"], headers={"Idempotent-Replayed": "true"}
            )
        try:
            status, content = code, await run()
        except EngineError as exc:
            status, content = exc.status_code, {"detail": str(exc)}
        except PlanValidationError as exc:
            status, content = 422, {"detail": "plan failed validation", "errors": exc.errors}
        except BaseException:
            store.release_idempotency_key(scope, key)
            raise
        store.complete_idempotency_key(scope, key, status, content)
        return JSONResponse(status_code=status, content=content)

    @app.post("/api/missions", status_code=201)
    async def create_mission(body: CreateMission, request: Request) -> JSONResponse:
        engine = engine_of(request)

        async def run() -> dict:
            # Returns at once with status "planning"; the stream announces when the plan is ready.
            mission_id = await engine.submit_mission(
                body.work_order, body.planner, body.actor, target=body.target, allow_repair=body.allow_repair
            )
            return engine.mission_detail(mission_id)

        return await idempotent(request, body, run, code=201)

    @app.get("/api/missions/{mission_id}")
    async def get_mission(mission_id: str, request: Request) -> dict:
        return engine_of(request).mission_detail(mission_id)

    @app.put("/api/missions/{mission_id}/plan")
    async def edit_plan(mission_id: str, body: EditPlan, request: Request) -> JSONResponse:
        engine = engine_of(request)

        async def run() -> dict:
            await engine.update_plan(mission_id, body.plan, body.actor)
            return engine.mission_detail(mission_id)

        return await idempotent(request, body, run)

    @app.post("/api/missions/{mission_id}/start")
    async def start(mission_id: str, body: ActorOnly, request: Request) -> JSONResponse:
        engine = engine_of(request)

        async def run() -> dict:
            await engine.start_mission(mission_id, body.actor)
            return engine.mission_detail(mission_id)

        return await idempotent(request, body, run)

    @app.post("/api/missions/{mission_id}/tasks/{task_id}/decision")
    async def decide(mission_id: str, task_id: str, body: Decision, request: Request) -> JSONResponse:
        engine = engine_of(request)

        async def run() -> dict:
            await engine.decide_approval(mission_id, task_id, body.decision, body.actor, body.comment)
            return engine.mission_detail(mission_id)

        return await idempotent(request, body, run)

    @app.post("/api/missions/{mission_id}/tasks/{task_id}/retry")
    async def retry(mission_id: str, task_id: str, body: ActorOnly, request: Request) -> JSONResponse:
        engine = engine_of(request)

        async def run() -> dict:
            await engine.retry_task(mission_id, task_id, body.actor)
            return engine.mission_detail(mission_id)

        return await idempotent(request, body, run)

    @app.post("/api/missions/{mission_id}/cancel")
    async def cancel(mission_id: str, body: ActorOnly, request: Request) -> JSONResponse:
        engine = engine_of(request)

        async def run() -> dict:
            await engine.cancel_mission(mission_id, body.actor)
            return engine.mission_detail(mission_id)

        return await idempotent(request, body, run)

    @app.post("/api/sim/robots/{robot_id}/fault")
    async def inject_fault(robot_id: str, body: Fault, request: Request) -> dict:
        if robot_id not in fleet.robots:
            raise HTTPException(404, f"robot {robot_id} not found")
        sim: SimulatedFleet = request.app.state.sim
        engine = engine_of(request)
        if body.fault == "offline":
            await sim.set_online(robot_id, False)
        elif body.fault == "online":
            await sim.set_online(robot_id, True)
            await engine.restore_robot(robot_id, body.actor)
        elif body.fault == "lose_silently":
            # Nothing is reported; the engine finds out when the heartbeat lease expires.
            sim.lose_silently(robot_id)
            engine._notify()
        elif body.fault == "clear":
            sim.clear_faults(robot_id)
            engine._notify()
        else:
            sim.arm_fault(robot_id, body.fault)
            engine._notify()
        return robots_response(request)

    @app.post("/api/sim/assets/{asset_id}")
    async def set_asset(asset_id: str, body: AssetScenario, request: Request) -> dict:
        if asset_id not in ASSETS:
            raise HTTPException(404, f"asset {asset_id} not found")
        request.app.state.sim.set_defect(asset_id, body.defect_present)
        engine_of(request)._notify()
        return request.app.state.sim.snapshot()

    @app.get("/api/metrics")
    async def metrics(request: Request) -> dict:
        return compute_metrics(request.app.state.store)

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        engine = engine_of(request)
        queue: asyncio.Queue = asyncio.Queue()
        engine.subscribers.add(queue)

        async def events():
            try:
                yield "event: hello\ndata: {}\n\n"
                while not await request.is_disconnected():
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(msg)}\n\n"
            finally:
                engine.subscribers.discard(queue)

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    dist = BACKEND_DIR.parent / "frontend" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")

    return app


app = create_app()
