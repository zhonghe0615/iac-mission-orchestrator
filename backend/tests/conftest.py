import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.catalog import Fleet
from app.engine import Engine
from app.planner import FixturePlanner
from app.simulator import SimulatedFleet
from app.store import Store

# Simulated durations and timeouts run at 1/100 of real time in tests.
TIME_SCALE = 0.01
BACKEND_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Harness:
    store: Store
    sim: SimulatedFleet
    engine: Engine

    def mission(self, mission_id: str) -> dict:
        return self.store.mission(mission_id)

    def task(self, mission_id: str, task_id: str) -> dict:
        return self.store.task(mission_id, task_id)

    def commands(self, action: str | None = None) -> list:
        return [c for c in self.sim.command_log if action is None or c.action == action]

    def kinds(self, mission_id: str) -> list[str]:
        return [e["kind"] for e in self.store.events(mission_id)]

    async def wait(self, predicate, timeout: float = 3.0, what: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.005)
        raise AssertionError(f"timed out waiting for {what}")

    async def wait_status(self, mission_id: str, *statuses: str, timeout: float = 3.0) -> None:
        await self.wait(
            lambda: self.mission(mission_id)["status"] in statuses,
            timeout,
            f"mission status in {statuses} (is {self.mission(mission_id)['status']})",
        )

    async def wait_task(self, mission_id: str, task_id: str, *statuses: str, timeout: float = 3.0) -> None:
        await self.wait(
            lambda: self.task(mission_id, task_id)["status"] in statuses,
            timeout,
            f"task {task_id} status in {statuses} (is {self.task(mission_id, task_id)['status']})",
        )

    async def started_mission(self) -> str:
        mission_id = await self.engine.create_mission("Inspect and resolve the suspected leak on tank T-101", "fixture", "tester")
        await self.engine.start_mission(mission_id, "tester")
        return mission_id


async def make_harness(
    db_path: str = ":memory:", planners: dict | None = None, fleet: Fleet | None = None, time_scale: float = TIME_SCALE
) -> Harness:
    fleet = fleet or Fleet.load()
    store = Store(db_path)
    sim = SimulatedFleet(fleet, time_scale=time_scale)
    engine = Engine(store, sim, fleet, planners or {"fixture": FixturePlanner(fleet)}, time_scale=time_scale)
    sim.attach(engine.enqueue, heartbeat=engine.heartbeat)
    await engine.start()
    sim.start()
    return Harness(store, sim, engine)


async def close_harness(h: Harness) -> None:
    await h.engine.stop()
    await h.sim.shutdown()
    h.store.close()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def h():
    harness = await make_harness()
    yield harness
    await close_harness(harness)
