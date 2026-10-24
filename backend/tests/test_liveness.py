"""Heartbeat liveness: robots that fail silently are detected by the lease, not only by command timeouts."""

import pytest

from app.catalog import ACTIONS
from app.robots import HEARTBEAT_LEASE_S

from .conftest import TIME_SCALE

pytestmark = pytest.mark.anyio

LEASE = HEARTBEAT_LEASE_S * TIME_SCALE
THERMAL_TIMEOUT = ACTIONS["thermal_inspection"].timeout_s * TIME_SCALE


def heartbeat_losses(h) -> list[dict]:
    return [e for e in h.store.events(kinds=("robot_status",)) if "no heartbeat" in e["message"]]


async def test_silent_loss_mid_inspection_is_detected_by_the_lease(h):
    mid = await h.started_mission()
    await h.wait(lambda: h.task(mid, "t1")["status"] == "running", what="t1 running")
    h.sim.lose_silently("scout-a")  # no status event is sent

    await h.wait_status(mid, "waiting_approval")
    first, second = h.store.task_attempts(mid, "t1")
    assert (first["robot_id"], first["status"]) == ("scout-a", "lost")
    assert "stopped sending heartbeats" in first["detail"]
    # Found by the lease, well before the command's own timeout would have fired.
    assert first["ended_at"] - first["started_at"] < THERMAL_TIMEOUT
    assert (second["robot_id"], second["status"]) == ("scout-b", "succeeded")
    assert h.store.robot("scout-a")["health_reason"] == f"no heartbeat for {HEARTBEAT_LEASE_S:g}s"


async def test_command_sent_to_a_silently_lost_robot_is_recovered(h):
    # The engine does not know yet, so it dispatches to Scout A; the command vanishes.
    h.sim.lose_silently("scout-a")
    mid = await h.started_mission()
    await h.wait_status(mid, "waiting_approval")
    first, second = h.store.task_attempts(mid, "t1")
    assert first["robot_id"] == "scout-a" and first["status"] == "lost"
    assert second["robot_id"] == "scout-b" and second["status"] == "succeeded"


async def test_idle_robot_silently_lost_then_heartbeats_resume(h):
    h.sim.lose_silently("scout-b")
    await h.wait(lambda: not h.store.robot("scout-b")["online"], timeout=1, what="scout-b marked offline")
    assert len(heartbeat_losses(h)) == 1

    # Work goes only to robots that are alive.
    mid = await h.started_mission()
    await h.wait_status(mid, "waiting_approval")
    assert {a["robot_id"] for a in h.store.attempts(mid)} == {"scout-a"}

    # The robot comes back without announcing it; resumed heartbeats are enough.
    robot = h.sim.robots["scout-b"]
    robot.online, robot.silent = True, False
    await h.wait(lambda: h.store.robot("scout-b")["online"], timeout=1, what="scout-b back online")
    assert any("heartbeats resumed" in e["message"] for e in h.store.events(kinds=("robot_status",)))


async def test_healthy_fleet_has_no_false_heartbeat_alarms(h):
    mid = await h.started_mission()
    await h.wait_status(mid, "waiting_approval")
    await h.engine.decide_approval(mid, "t3", "approve", "tester", "")
    await h.wait_status(mid, "resolved")
    assert heartbeat_losses(h) == []
