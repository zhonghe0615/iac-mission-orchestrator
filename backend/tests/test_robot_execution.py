"""Mock robot execution: route recipes, contract validation, progress events, and their safety rules."""

import asyncio
import json
import math
import time

import pytest

from app.catalog import Fleet
from app.mock_robot import DEFAULT_ROUTES_FILE, CaptureRequest, MockRejected, MockRobotAPI, NavigateRequest, SiteRoutes, parse_request
from app.robots import RobotEvent

from .conftest import close_harness, make_harness

pytestmark = pytest.mark.anyio

T101_ROUTE = ["T101-south-access", "T101-east-seam", "T101-north-shell", "T101-west-shell"]


def ops(h, command_id: str, kind: str | None = None) -> list[dict]:
    return [e for e in h.sim.api.operations_for(command_id) if kind is None or e["kind"] == kind]


def requests(h, command_id: str, operation: str | None = None) -> list[dict]:
    return [e for e in ops(h, command_id, "request") if operation is None or e["operation"] == operation]


def progress(h, mission_id: str, task_id: str | None = None) -> list[dict]:
    return [
        e for e in h.store.events(mission_id)
        if e["kind"] == "robot_progress" and (task_id is None or e["data"]["task_id"] == task_id)
    ]


async def test_thermal_inspection_walks_the_route_and_reports_samples(h):
    mid = await h.started_mission()
    await h.wait_status(mid, "waiting_approval")
    (attempt,) = h.store.task_attempts(mid, "t1")

    # One parent attempt visits every station in order, capturing only after confirmed arrival.
    trace = ops(h, attempt["id"])
    assert [r["waypoint_id"] for r in requests(h, attempt["id"], "navigate_to")] == T101_ROUTE
    for station in T101_ROUTE:
        reached = next(i for i, e in enumerate(trace) if e.get("status") == "reached_goal" and e["waypoint_id"] == station)
        captured = next(i for i, e in enumerate(trace) if e["kind"] == "request" and e["operation"] == "capture" and e["waypoint_id"] == station)
        assert reached < captured

    # The finding comes from the samples: only the east seam is anomalous.
    (evidence,) = [e for e in h.store.evidence(mid) if e["task_id"] == "t1"]
    data = evidence["data"]
    assert [s["waypoint_id"] for s in data["samples"]] == T101_ROUTE
    assert [s["waypoint_id"] for s in data["samples"] if s["temperature_delta_c"] >= data["anomaly_threshold_c"]] == ["T101-east-seam"]
    assert data["location"] == "east weld seam" and data["max_temperature_delta_c"] == 18.4
    assert data["source"] == "simulated-thermal-payload" and data["simulated"] is True
    assert data["map_id"] == "north-tank-farm-v1" and data["route_id"] == "T101-perimeter"
    assert h.task(mid, "t1")["finding"] == "defect_detected"

    # Progress is audited per step, in order, and never created evidence of its own.
    steps = [(e["data"]["progress"]["step"], e["data"]["progress"]["stage"]) for e in progress(h, mid, "t1")]
    assert steps[:3] == [(1, "navigating"), (1, "arrived"), (2, "captured")]
    assert len(steps) == 12 and len(h.store.evidence(mid)) == 2


async def test_no_defect_runs_the_same_route_with_normal_readings(h):
    h.sim.set_defect("tank-T101", False)
    mid = await h.started_mission()
    await h.wait_status(mid, "no_action_needed")
    (evidence,) = [e for e in h.store.evidence(mid) if e["task_id"] == "t1"]
    assert len(evidence["data"]["samples"]) == 4
    assert all(s["temperature_delta_c"] < evidence["data"]["anomaly_threshold_c"] for s in evidence["data"]["samples"])
    assert "location" not in evidence["data"]
    assert not [e for e in h.sim.api.trace if e.get("operation") == "run_procedure"]


async def test_t102_uses_only_its_own_route():
    h = await make_harness()
    try:
        h.sim.set_defect("tank-T102", True)
        plan = {
            "summary": "Inspect T-102",
            "target": "tank-T102",
            "tasks": [{"id": "t1", "action": "thermal_inspection", "args": {"target": "tank-T102"}}],
        }
        mid = await h.engine.create_mission("Inspect tank T-102", "fixture", "tester", target="tank-T102", allow_repair=False)
        await h.engine.update_plan(mid, plan, "tester")
        await h.engine.start_mission(mid, "tester")
        await h.wait_status(mid, "unresolved")
        (attempt,) = h.store.task_attempts(mid, "t1")
        waypoints = {e["waypoint_id"] for e in ops(h, attempt["id"]) if "waypoint_id" in e}
        assert waypoints and all(w.startswith("T102-") for w in waypoints)
        (evidence,) = h.store.evidence(mid)
        assert evidence["data"]["route_id"] == "T102-perimeter"
        assert all(s["waypoint_id"].startswith("T102-") for s in evidence["data"]["samples"])
    finally:
        await close_harness(h)


class CountingWorld:
    def __init__(self):
        self.calls: list[tuple] = []

    def thermal_delta(self, target, waypoint_id):
        self.calls.append(("thermal", target, waypoint_id))
        return 1.0

    def visual_observation(self, target, waypoint_id):
        self.calls.append(("visual", target, waypoint_id))
        return "ok"

    def apply_procedure(self, target, procedure):
        self.calls.append(("procedure", target, procedure))


def mock_api() -> tuple[MockRobotAPI, CountingWorld]:
    world = CountingWorld()
    return MockRobotAPI(Fleet.load(), SiteRoutes.load(), world), world


def op(kind: str, waypoint: str = "T101-east-seam", robot: str = "scout-a", n: int = 0, **extra) -> dict:
    base = {
        "operation_id": f"cmd-test:{n}",
        "parent_command_id": "cmd-test",
        "robot_id": robot,
        "target": "tank-T101",
        "map_id": "north-tank-farm-v1",
        "waypoint_id": waypoint,
        "deadline_at": time.time() + 5,
    }
    if kind == "navigate":
        wp = SiteRoutes.load().waypoint(waypoint)
        pose = {"frame_id": "site", "target_position": list(wp.position) if wp else [0.0, 0.0, 0.0]}
        pose["target_yaw_rad"] = wp.yaw_rad if wp else None
        return {**base, "operation": "navigate_to", **pose, **extra}
    if kind == "capture":
        return {**base, "operation": "capture", "source": "simulated-thermal-payload", **extra}
    return {**base, "operation": "run_procedure", "procedure": "seal_weld_seam", **extra}


async def no_feedback(_):
    pass


async def test_mock_contract_rejects_malformed_and_unsupported_requests():
    for raw in (
        {**op("navigate"), "operation": "jump"},  # unknown operation
        op("capture", source="lidar"),  # unsupported sensor source
        op("navigate", joint_angles=[0.1]),  # unknown field
        op("navigate", target_position=[6.0, 0.0]),  # not a 3D position
        op("navigate", target_position=[6.0, 0.0, math.inf]),
        {k: v for k, v in op("navigate").items() if k != "waypoint_id"},  # missing field
    ):
        with pytest.raises(MockRejected, match="malformed"):
            parse_request(raw)

    api, world = mock_api()
    rejected = [
        (op("navigate", waypoint="T102-east-seam"), "not on the route"),  # cross-asset waypoint
        (op("navigate", waypoint="T101-roof"), "not on the route"),  # unknown waypoint
        (op("navigate", map_id="other-map"), "unknown map"),
        (op("capture"), "not confirmed arrival"),  # capture before arrival
        (op("procedure", robot="manipulator-c"), "not confirmed arrival"),
        (op("capture", robot="manipulator-c", source="simulated-thermal-payload"), "no thermal_inspection"),
        (op("procedure"), "no repair capability"),  # scouts cannot repair
        (op("navigate", deadline_at=time.time() - 1), "deadline"),
        (op("navigate", frame_id="odom"), "unknown frame"),
        (op("navigate", target_position=[6.0, 0.5, 0.0]), "target pose does not match"),  # wrong place for the ID
        (op("navigate", target_yaw_rad=0.0), "target pose does not match"),
    ]
    for raw, reason in rejected:
        req = parse_request(raw)
        with pytest.raises(MockRejected, match=reason):
            if isinstance(req, NavigateRequest):
                await api.navigate(req, 0.0, no_feedback)
            elif isinstance(req, CaptureRequest):
                await api.capture(req, 0.0)
            else:
                await api.run_procedure(req, 0.0)
    assert world.calls == []
    assert all(api.position[r] is None for r in api.position)
    assert not [e for e in api.trace if e["kind"] == "ack" and e["accepted"]]

    procedure = op("procedure", robot="manipulator-c", procedure="cut_pipe")
    with pytest.raises(MockRejected, match="malformed|allowlisted"):
        await api.run_procedure(parse_request(procedure), 0.0)


async def test_repeated_operation_ids_do_not_repeat_side_effects():
    api, world = mock_api()
    nav = parse_request(op("navigate", robot="manipulator-c", n=0))
    procedure = parse_request(op("procedure", robot="manipulator-c", n=1))
    capture_nav = parse_request(op("navigate", n=2))
    capture = parse_request(op("capture", n=3))
    await api.navigate(nav, 0.0, no_feedback)
    first = await api.run_procedure(procedure, 0.0)
    assert await api.run_procedure(procedure, 0.0) == first
    await api.navigate(capture_nav, 0.0, no_feedback)
    reading = await api.capture(capture, 0.0)
    assert await api.capture(capture, 0.0) == reading
    assert world.calls == [("procedure", "tank-T101", "seal_weld_seam"), ("thermal", "tank-T101", "T101-east-seam")]
    assert len([e for e in api.trace if e["kind"] == "duplicate_ignored"]) == 2


async def test_navigation_failure_stops_the_route_and_retries_elsewhere(h):
    h.sim.arm_fault("scout-a", "fail_next")
    mid = await h.started_mission()
    await h.wait_status(mid, "waiting_approval")
    failed, succeeded = h.store.task_attempts(mid, "t1")
    assert (failed["robot_id"], failed["status"]) == ("scout-a", "failed")
    assert "navigation to west shell failed" in failed["detail"]

    # No capture at the failed station; the earlier captures stay with the failed attempt.
    captures = requests(h, failed["id"], "capture")
    assert [c["waypoint_id"] for c in captures] == T101_ROUTE[:3]
    assert {e["data"]["attempt_id"] for e in progress(h, mid, "t1")} == {failed["id"], succeeded["id"]}

    # Only the successful attempt supplies workflow evidence.
    assert (succeeded["robot_id"], succeeded["status"]) == ("scout-b", "succeeded")
    assert [e["attempt_id"] for e in h.store.evidence(mid) if e["task_id"] == "t1"] == [succeeded["id"]]
    assert len(requests(h, succeeded["id"], "capture")) == 4


async def test_robot_lost_mid_route_starts_a_new_attempt_on_another_robot(h):
    mid = await h.started_mission()
    await h.wait(
        lambda: any(e["data"]["progress"]["waypoint_id"] == "T101-east-seam" and e["data"]["progress"]["stage"] == "captured" for e in progress(h, mid, "t1")),
        what="east seam captured",
    )
    await h.sim.set_online("scout-a", False)
    await h.wait_status(mid, "waiting_approval")
    lost, replacement = h.store.task_attempts(mid, "t1")
    assert lost["status"] == "lost" and replacement["robot_id"] == "scout-b"
    assert len(requests(h, lost["id"], "capture")) < 4
    # The replacement walks the whole route again with its own operation IDs.
    assert [r["waypoint_id"] for r in requests(h, replacement["id"], "navigate_to")] == T101_ROUTE
    assert [e["attempt_id"] for e in h.store.evidence(mid) if e["task_id"] == "t1"] == [replacement["id"]]


async def test_duplicate_stale_and_misaddressed_progress_is_ignored(h):
    mid = await h.started_mission()
    await h.wait(lambda: len(progress(h, mid, "t1")) >= 2, what="t1 progress")
    (attempt,) = h.store.task_attempts(mid, "t1")
    first = progress(h, mid, "t1")[0]["data"]["progress"]

    def event(event_id: str, robot: str, seq: int) -> RobotEvent:
        return RobotEvent(event_id, robot, "progress", attempt["id"], {**first, "seq": seq})

    await h.engine.enqueue(event("ev-regressive", "scout-a", 1))  # an earlier step arriving late
    await h.engine.enqueue(event("ev-wrong-robot", "scout-b", 999))
    await h.engine.enqueue(event("ev-dup", "scout-a", 998))
    await h.engine.enqueue(event("ev-dup", "scout-a", 998))
    await h.wait(lambda: "duplicate_event_ignored" in h.kinds(mid), what="duplicate ignored")
    ignored = [e["message"] for e in h.store.events(mid) if e["kind"] == "stale_event_ignored"]
    assert any("repeated or out of order" in m for m in ignored)
    assert any("attempt runs on Scout A" in m for m in ignored)
    # Only the one well-formed new step was accepted.
    assert len([e for e in progress(h, mid, "t1") if e["data"]["progress"]["seq"] == 998]) == 1

    await h.wait_status(mid, "waiting_approval")
    accepted = len(progress(h, mid, "t1"))
    await h.engine.enqueue(event("ev-late", "scout-a", 10_000))  # attempt already succeeded
    await h.wait(lambda: any("already succeeded" in e["message"] for e in h.store.events(mid)), what="late progress ignored")
    assert len(progress(h, mid, "t1")) == accepted
    assert h.task(mid, "t1")["status"] == "succeeded" and len(h.store.evidence(mid)) == 2


async def test_cancel_during_route_stops_remaining_operations(h):
    mid = await h.started_mission()
    await h.wait(lambda: any(e["data"]["progress"]["stage"] == "arrived" for e in progress(h, mid, "t1")), what="first arrival")
    (attempt,) = h.store.task_attempts(mid, "t1")
    await h.engine.cancel_mission(mid, "tester")
    await h.wait(lambda: h.store.task_attempts(mid, "t1")[0]["status"] == "cancelled", what="cancel confirmed")
    sent = len(requests(h, attempt["id"]))
    await asyncio.sleep(0.1)  # longer than the whole route would take at test speed
    assert len(requests(h, attempt["id"])) == sent < 8
    assert attempt["id"] not in h.sim.robots["scout-a"].results  # no completion was produced
    assert h.store.evidence(mid) == []


async def test_denied_repair_sends_no_intervention_and_approved_repair_is_verified(h):
    denied = await h.started_mission()
    await h.wait_status(denied, "waiting_approval")
    await h.engine.decide_approval(denied, "t3", "reject", "tester", "")
    await h.wait_status(denied, "unresolved")
    assert not [e for e in h.sim.api.trace if e.get("operation") == "run_procedure"]
    assert not h.sim.assets["tank-T101"].repaired

    approved = await h.started_mission()
    await h.wait_status(approved, "waiting_approval")
    await h.engine.decide_approval(approved, "t3", "approve", "tester", "")
    await h.wait_status(approved, "resolved")
    (repair,) = h.store.task_attempts(approved, "t3")
    assert [r["operation"] for r in requests(h, repair["id"])] == ["navigate_to", "run_procedure"]
    (verify,) = h.store.task_attempts(approved, "t4")
    (fresh,) = [e for e in h.store.evidence(approved) if e["task_id"] == "t4"]
    assert fresh["data"]["samples"][0]["temperature_delta_c"] < fresh["data"]["anomaly_threshold_c"]
    assert fresh["data"]["samples"][0]["acquired_at"] > repair["ended_at"] - 1
    assert requests(h, verify["id"], "capture")[0]["waypoint_id"] == "T101-east-seam"


async def test_restart_mid_route_keeps_progress_and_starts_a_new_route(tmp_path):
    db = str(tmp_path / "orchestrator.db")
    first = await make_harness(db)
    mid = await first.started_mission()
    await first.wait(lambda: len(progress(first, mid, "t1")) >= 3, what="mid-route progress")
    (interrupted,) = first.store.task_attempts(mid, "t1")
    await close_harness(first)

    second = await make_harness(db)
    try:
        assert {e["data"]["attempt_id"] for e in progress(second, mid, "t1")} == {interrupted["id"]}
        await second.wait_status(mid, "waiting_approval")
        lost, retry = second.store.task_attempts(mid, "t1")
        assert lost["status"] == "lost"
        # No waypoint resume: the new attempt starts the route from the first station.
        assert requests(second, retry["id"], "navigate_to")[0]["waypoint_id"] == T101_ROUTE[0]
    finally:
        await close_harness(second)


def write_routes(tmp_path, change) -> str:
    data = json.loads(DEFAULT_ROUTES_FILE.read_text())
    change(data)
    path = tmp_path / "routes.json"
    # allow_nan keeps NaN and Infinity in the file, as a hand-edited config might.
    path.write_text(json.dumps(data, allow_nan=True))
    return str(path)


def first_waypoint(data: dict) -> dict:
    return data["waypoints"][0]


@pytest.mark.parametrize(
    "position",
    [[0.0, -6.0], [0.0, -6.0, 0.0, 1.0], [0.0, float("nan"), 0.0], [float("inf"), 0.0, 0.0], ["0", -6.0, 0.0], [True, -6.0, 0.0], None],
)
def test_waypoint_position_must_be_three_finite_numbers(tmp_path, position):
    path = write_routes(tmp_path, lambda d: first_waypoint(d).__setitem__("position", position))
    with pytest.raises(ValueError, match="position"):
        SiteRoutes.load(path)


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda d: d.pop("frame_id"), "frame_id"),
        (lambda d: d.__setitem__("units", "ft"), "units"),
        (lambda d: d.__setitem__("synthetic", False), "synthetic"),
        (lambda d: first_waypoint(d).__setitem__("yaw_rad", 7.0), "yaw_rad"),
        (lambda d: first_waypoint(d).__setitem__("yaw_rad", float("nan")), "yaw_rad"),
        (lambda d: d["assets"]["tank-T101"]["stations"].append("T101-roof"), "undefined waypoint 'T101-roof'"),
        (lambda d: d["assets"]["tank-T101"]["stations"].append("T102-east-seam"), "used by both"),
        (lambda d: d["assets"]["tank-T101"].__setitem__("service_waypoint", "nowhere"), "undefined waypoint 'nowhere'"),
        (lambda d: d["waypoints"].append(dict(first_waypoint(d))), "defined more than once"),
        (lambda d: d["assets"].pop("tank-T102"), "asset 'tank-T102' has no inspection route"),
    ],
)
def test_route_config_is_validated(tmp_path, change, message):
    with pytest.raises(ValueError, match=message):
        SiteRoutes.load(write_routes(tmp_path, change))


def test_shipped_routes_are_synthetic_with_a_declared_frame():
    routes = SiteRoutes.load()
    assert (routes.frame_id, routes.units, routes.synthetic) == ("site", "m", True)
    assert routes.assets["tank-T101"].stations == T101_ROUTE
    assert all(len(w.position) == 3 and w.yaw_rad is not None for w in routes.waypoints)


async def test_navigation_request_and_feedback_carry_the_pose(h):
    routes = SiteRoutes.load()
    mid = await h.started_mission()
    await h.wait_status(mid, "waiting_approval")
    await h.engine.decide_approval(mid, "t3", "approve", "tester", "")
    await h.wait_status(mid, "resolved")

    (thermal,) = h.store.task_attempts(mid, "t1")
    for nav in requests(h, thermal["id"], "navigate_to"):
        wp = routes.waypoint(nav["waypoint_id"])
        assert nav["frame_id"] == "site"
        assert nav["target_position"] == wp.position and nav["target_yaw_rad"] == wp.yaw_rad
        (reached,) = [f for f in ops(h, thermal["id"], "feedback") if f["operation_id"] == nav["operation_id"] and f["status"] == "reached_goal"]
        assert reached["position"] == wp.position and reached["yaw_rad"] == wp.yaw_rad and reached["frame_id"] == "site"

    # Repair goes straight to the service waypoint: one navigation, no inspection route.
    (repair,) = h.store.task_attempts(mid, "t3")
    (nav,) = requests(h, repair["id"], "navigate_to")
    assert nav["waypoint_id"] == "T101-east-seam" and nav["target_position"] == (6.0, 0.0, 0.0)

    # The engine audits the reported arrival pose; it never needed the map to do so.
    arrived = [e for e in progress(h, mid, "t1") if e["data"]["progress"]["stage"] == "arrived"]
    assert [e["data"]["progress"]["position"] for e in arrived] == [list(routes.waypoint(w).position) for w in T101_ROUTE]
    assert "reached east weld seam at (6, 0, 0) in frame site" in arrived[1]["message"]
