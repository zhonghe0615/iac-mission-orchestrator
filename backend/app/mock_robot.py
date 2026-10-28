"""Mock robot API: navigation, sensor capture, and a simulated procedure.

These are IAC-owned mock contracts, shaped after Boston Dynamics Spot concepts:
navigation commands are acknowledged separately from their feedback (GraphNav),
routes run over a map of identified waypoints, and sensing is a separate payload
service that reports its source and acquisition time. They are not Spot request
bodies or an SDK implementation. See docs/ROBOT_INTEGRATION.md.

Only the integration adapter (simulator.py) talks to this API. The engine never
sees waypoints, maps, or operation IDs; it receives normalized progress and
results.
"""

import asyncio
import json
import math
import time
from pathlib import Path
from typing import Annotated, Awaitable, Callable, Literal, Protocol, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .catalog import ACTIONS, ASSETS, Fleet

DEFAULT_ROUTES_FILE = Path(__file__).resolve().parent.parent / "config" / "inspection_routes.json"

THERMAL_SOURCE = "simulated-thermal-payload"
CAMERA_SOURCE = "simulated-camera"
PROCEDURES = ACTIONS["repair"].args["procedure"]
# The robot capability each operation or sensor source needs.
REQUIRED_CAPABILITY = {
    "navigate_to": "navigation",
    "run_procedure": "repair",
    THERMAL_SOURCE: "thermal_inspection",
    CAMERA_SOURCE: "camera",
}


class MockRejected(Exception):
    """The mock robot refused an operation; nothing was executed."""


# Site routes


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# A finite number; strict mode refuses strings and booleans that lax parsing would coerce.
Finite = Annotated[float, Field(strict=True, allow_inf_nan=False)]
# [x, y, z] in the map's frame and units: exactly three finite numbers.
Position = tuple[Finite, Finite, Finite]


class Waypoint(_Strict):
    """One navigation destination: where the robot stands, and optionally which way it faces.

    The position is the robot's standing position on the ground. It is not the sensor's
    mounting height and not the point on the tank being inspected or repaired.
    """

    waypoint_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    position: Position
    # Arrival heading about +z, for stations where the robot must face the tank.
    yaw_rad: Finite | None = Field(default=None, ge=-math.pi - 1e-3, le=math.pi + 1e-3)


class AssetRoute(_Strict):
    """An ordered sequence of inspection stations (waypoint IDs) around one asset."""

    route_id: str = Field(min_length=1)
    stations: list[str] = Field(min_length=1)
    # Where close-range work happens: visual capture, repair, and verification. Repair
    # navigates straight here rather than walking the inspection route.
    service_waypoint: str


class SiteRoutes(_Strict):
    map_id: str = Field(min_length=1)
    version: int
    note: str = ""
    frame_id: str = Field(min_length=1)
    units: Literal["m"]
    # Required and must be true: these coordinates are made up for the demo.
    synthetic: Literal[True]
    waypoints: list[Waypoint] = Field(min_length=1)
    assets: dict[str, AssetRoute]

    @classmethod
    def load(cls, path: str | Path = DEFAULT_ROUTES_FILE) -> "SiteRoutes":
        try:
            routes = cls.model_validate(json.loads(Path(path).read_text()))
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise ValueError(f"invalid inspection routes file {path}: {exc}") from None
        routes.check()
        return routes

    def check(self) -> None:
        ids = [w.waypoint_id for w in self.waypoints]
        errors = [f"waypoint '{w}' is defined more than once" for w in ids if ids.count(w) > 1]
        errors += [f"asset '{a}' has no inspection route" for a in ASSETS if a not in self.assets]
        errors += [f"route for unknown asset '{a}'" for a in self.assets if a not in ASSETS]
        owner: dict[str, str] = {}
        for asset_id, route in self.assets.items():
            used = route.stations + [route.service_waypoint]
            errors += [f"route for '{asset_id}' visits '{w}' more than once" for w in route.stations if route.stations.count(w) > 1]
            errors += [f"route for '{asset_id}' uses undefined waypoint '{w}'" for w in used if w not in ids]
            for w in used:
                if owner.setdefault(w, asset_id) != asset_id:
                    errors.append(f"waypoint '{w}' is used by both '{owner[w]}' and '{asset_id}'")
        if errors:
            raise ValueError("invalid inspection routes: " + "; ".join(sorted(set(errors))))

    def waypoint(self, waypoint_id: str) -> Waypoint | None:
        return next((w for w in self.waypoints if w.waypoint_id == waypoint_id), None)

    def station(self, target: str, waypoint_id: str) -> Waypoint | None:
        """The waypoint, if it belongs to the target asset (a route station or its service waypoint)."""
        route = self.assets.get(target)
        if route is None or waypoint_id not in (*route.stations, route.service_waypoint):
            return None
        return self.waypoint(waypoint_id)


# Requests


class _Operation(_Strict):
    operation_id: str = Field(min_length=1)
    # The orchestrator's command ID: one execution attempt.
    parent_command_id: str = Field(min_length=1)
    robot_id: str
    target: str
    map_id: str
    waypoint_id: str
    deadline_at: float


class NavigateRequest(_Operation):
    operation: Literal["navigate_to"] = "navigate_to"
    # The destination, in the map's frame. The adapter names where to go; the robot's own
    # navigation layer decides how to get there.
    frame_id: str
    target_position: Position
    target_yaw_rad: Finite | None = None


class CaptureRequest(_Operation):
    operation: Literal["capture"] = "capture"
    source: Literal["simulated-thermal-payload", "simulated-camera"]


class ProcedureRequest(_Operation):
    operation: Literal["run_procedure"] = "run_procedure"
    procedure: str


Request = Annotated[Union[NavigateRequest, CaptureRequest, ProcedureRequest], Field(discriminator="operation")]
_REQUEST = TypeAdapter(Request)


def parse_request(raw: dict) -> NavigateRequest | CaptureRequest | ProcedureRequest:
    try:
        return _REQUEST.validate_python(raw)
    except ValidationError as exc:
        raise MockRejected(f"malformed request: {exc.errors()[0]['msg']}") from None


# Acknowledgements, feedback, and responses


class Ack(_Strict):
    operation_id: str
    accepted: bool
    reason: str = ""


class NavigationFeedback(_Strict):
    operation_id: str
    robot_id: str
    waypoint_id: str
    # Acceptance is not arrival: only reached_goal confirms the robot is at the waypoint.
    status: Literal["running", "reached_goal", "failed", "cancelled"]
    detail: str = ""
    # The robot's reported position and heading; set on reached_goal.
    frame_id: str | None = None
    position: Position | None = None
    yaw_rad: float | None = None
    at: float


class CaptureResponse(_Strict):
    operation_id: str
    parent_command_id: str
    robot_id: str
    status: Literal["completed", "failed"]
    source: str
    target: str
    waypoint_id: str
    acquired_at: float
    temperature_delta_c: float | None = None
    observation: str | None = None
    detail: str = ""
    simulated: Literal[True] = True


class ProcedureResponse(_Strict):
    operation_id: str
    parent_command_id: str
    robot_id: str
    status: Literal["completed", "failed"]
    procedure: str
    target: str
    waypoint_id: str
    completed_at: float
    detail: str = ""
    simulated: Literal[True] = True


class World(Protocol):
    """The simulated physical state the mock sensors and procedure act on."""

    def thermal_delta(self, target: str, waypoint_id: str) -> float: ...

    def visual_observation(self, target: str, waypoint_id: str) -> str: ...

    def apply_procedure(self, target: str, procedure: str) -> None: ...


FeedbackSink = Callable[[NavigationFeedback], Awaitable[None]]


class MockRobotAPI:
    """Stateful mock of every robot's navigation, sensor, and procedure services.

    Each operation is validated, acknowledged, and recorded in `trace`. A repeated
    operation ID returns the original response without repeating the side effect.
    """

    def __init__(self, fleet: Fleet, routes: SiteRoutes, world: World):
        self.fleet = fleet
        self.routes = routes
        self.world = world
        # Every request, acknowledgement, feedback, and response, in order.
        self.trace: list[dict] = []
        self._done: dict[str, BaseModel] = {}
        # Last waypoint each robot confirmed reaching; None while moving or unknown.
        self.position: dict[str, str | None] = {rid: None for rid in fleet.robots}

    def _record(self, kind: str, model: BaseModel) -> None:
        self.trace.append({"kind": kind, **model.model_dump()})

    def _check(self, req: NavigateRequest | CaptureRequest | ProcedureRequest) -> None:
        robot = self.fleet.robots.get(req.robot_id)
        if robot is None:
            raise MockRejected(f"unknown robot '{req.robot_id}'")
        need = REQUIRED_CAPABILITY[req.source if isinstance(req, CaptureRequest) else req.operation]
        if need not in robot.capabilities:
            raise MockRejected(f"{req.robot_id} has no {need} capability")
        if req.map_id != self.routes.map_id:
            raise MockRejected(f"unknown map '{req.map_id}'")
        if req.target not in self.routes.assets:
            raise MockRejected(f"no route for target '{req.target}'")
        waypoint = self.routes.station(req.target, req.waypoint_id)
        if waypoint is None:
            raise MockRejected(f"waypoint '{req.waypoint_id}' is not on the route for {req.target}")
        if isinstance(req, NavigateRequest):
            if req.frame_id != self.routes.frame_id:
                raise MockRejected(f"unknown frame '{req.frame_id}'")
            if req.target_position != waypoint.position or req.target_yaw_rad != waypoint.yaw_rad:
                raise MockRejected(f"target pose does not match waypoint {req.waypoint_id} on map {self.routes.map_id}")
        if isinstance(req, ProcedureRequest) and req.procedure not in PROCEDURES:
            raise MockRejected(f"procedure '{req.procedure}' is not allowlisted")
        if not isinstance(req, NavigateRequest) and self.position[req.robot_id] != req.waypoint_id:
            raise MockRejected(f"{req.robot_id} has not confirmed arrival at {req.waypoint_id}")
        if time.time() >= req.deadline_at:
            raise MockRejected("deadline already passed")

    def _accept(self, req) -> BaseModel | None:
        """Validate and acknowledge; returns the earlier response for a repeated operation."""
        self.trace.append({"kind": "request", **req.model_dump()})
        if req.operation_id in self._done:
            self.trace.append({"kind": "duplicate_ignored", "operation_id": req.operation_id})
            return self._done[req.operation_id]
        try:
            self._check(req)
        except MockRejected as exc:
            self._record("ack", Ack(operation_id=req.operation_id, accepted=False, reason=str(exc)))
            raise
        self._record("ack", Ack(operation_id=req.operation_id, accepted=True))
        return None

    async def _work(self, req, duration: float) -> bool:
        """Spend the operation's time, bounded by its deadline. False if the deadline ran out."""
        remaining = req.deadline_at - time.time()
        await asyncio.sleep(max(0.0, min(duration, remaining)))
        return duration <= remaining

    async def navigate(
        self, req: NavigateRequest, duration: float, on_feedback: FeedbackSink, fail: str | None = None
    ) -> NavigationFeedback:
        if (earlier := self._accept(req)) is not None:
            return earlier

        async def feedback(status: str, detail: str = "", **pose) -> NavigationFeedback:
            fb = NavigationFeedback(
                operation_id=req.operation_id,
                robot_id=req.robot_id,
                waypoint_id=req.waypoint_id,
                status=status,
                detail=detail,
                at=time.time(),
                **pose,
            )
            self._record("feedback", fb)
            return fb

        self.position[req.robot_id] = None
        await on_feedback(await feedback("running"))
        try:
            in_time = await self._work(req, duration)
        except asyncio.CancelledError:
            await feedback("cancelled")
            raise
        if fail or not in_time:
            result = await feedback("failed", fail or "deadline exceeded before arrival")
        else:
            self.position[req.robot_id] = req.waypoint_id
            # Synthetic travel ends exactly on the target; a real robot reports its localized pose.
            result = await feedback(
                "reached_goal", frame_id=req.frame_id, position=req.target_position, yaw_rad=req.target_yaw_rad
            )
        self._done[req.operation_id] = result
        return result

    async def capture(self, req: CaptureRequest, duration: float) -> CaptureResponse:
        if (earlier := self._accept(req)) is not None:
            return earlier
        in_time = await self._work(req, duration)
        reading: dict = {}
        if in_time and req.source == THERMAL_SOURCE:
            reading["temperature_delta_c"] = self.world.thermal_delta(req.target, req.waypoint_id)
        elif in_time:
            reading["observation"] = self.world.visual_observation(req.target, req.waypoint_id)
        result = CaptureResponse(
            operation_id=req.operation_id,
            parent_command_id=req.parent_command_id,
            robot_id=req.robot_id,
            status="completed" if in_time else "failed",
            source=req.source,
            target=req.target,
            waypoint_id=req.waypoint_id,
            acquired_at=time.time(),
            detail="" if in_time else "deadline exceeded before capture",
            **reading,
        )
        self._record("response", result)
        self._done[req.operation_id] = result
        return result

    async def run_procedure(self, req: ProcedureRequest, duration: float) -> ProcedureResponse:
        if (earlier := self._accept(req)) is not None:
            return earlier
        in_time = await self._work(req, duration)
        if in_time:
            # The simulated asset changes only when the procedure actually completes.
            self.world.apply_procedure(req.target, req.procedure)
        result = ProcedureResponse(
            operation_id=req.operation_id,
            parent_command_id=req.parent_command_id,
            robot_id=req.robot_id,
            status="completed" if in_time else "failed",
            procedure=req.procedure,
            target=req.target,
            waypoint_id=req.waypoint_id,
            completed_at=time.time(),
            detail="" if in_time else "deadline exceeded before the procedure finished",
        )
        self._record("response", result)
        self._done[req.operation_id] = result
        return result

    def operations_for(self, command_id: str) -> list[dict]:
        """The trace entries for one orchestrator command, for evidence and audit."""
        prefix = f"{command_id}:"
        return [e for e in self.trace if e.get("operation_id", "").startswith(prefix)]
