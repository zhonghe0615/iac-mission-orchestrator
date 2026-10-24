"""Fleet model: typed robots from configuration, new robot types, and behavior at larger scale."""

import time

import pytest

from app.catalog import Fleet, FleetConfigError

from .conftest import close_harness, make_harness

pytestmark = pytest.mark.anyio

QUADRUPED = {"name": "Inspection quadruped", "capabilities": ["navigation", "camera", "thermal_inspection"]}
MANIPULATOR = {"name": "Mobile manipulator", "capabilities": ["navigation", "camera", "repair"]}


def fleet_of(scouts: int, manipulators: int, extra_types: dict | None = None, extra_robots: list | None = None) -> Fleet:
    robots = [{"id": f"scout-{i:04d}", "type": "inspection_quadruped"} for i in range(scouts)]
    robots += [{"id": f"manip-{i:04d}", "type": "mobile_manipulator"} for i in range(manipulators)]
    return Fleet.from_dict(
        {
            "robot_types": {"inspection_quadruped": QUADRUPED, "mobile_manipulator": MANIPULATOR, **(extra_types or {})},
            "robots": robots + (extra_robots or []),
        }
    )


def test_default_fleet_file_declares_typed_robots():
    fleet = Fleet.load()
    assert {r.id: r.type.id for r in fleet.robots.values()} == {
        "scout-a": "inspection_quadruped",
        "scout-b": "inspection_quadruped",
        "manipulator-c": "mobile_manipulator",
    }


def test_invalid_fleet_config_lists_every_problem():
    with pytest.raises(FleetConfigError) as exc:
        Fleet.from_dict(
            {
                "robot_types": {"quadruped": QUADRUPED, "empty": {"capabilities": []}},
                "robots": [
                    {"id": "r1", "type": "quadruped"},
                    {"id": "r1", "type": "quadruped"},
                    {"id": "r2", "type": "drone"},
                    {"id": "r3"},
                ],
            }
        )
    message = str(exc.value)
    assert "duplicate robot id 'r1'" in message
    assert "robot 'r2' has unknown or missing type 'drone'" in message
    assert "robot 'r3' has unknown or missing type 'None'" in message
    assert "robot type 'empty' declares no capabilities" in message


async def test_new_robot_type_needs_only_configuration():
    # A thermal drone type, not known to any code, takes over inspection work.
    fleet = Fleet.from_dict(
        {
            "robot_types": {
                "thermal_drone": {"name": "Thermal drone", "capabilities": ["navigation", "thermal_inspection"]},
                "mobile_manipulator": MANIPULATOR,
            },
            "robots": [
                {"id": "drone-1", "name": "Drone 1", "type": "thermal_drone"},
                {"id": "manip-1", "name": "Manipulator 1", "type": "mobile_manipulator"},
            ],
        }
    )
    h = await make_harness(fleet=fleet)
    try:
        mid = await h.started_mission()
        await h.wait_status(mid, "waiting_approval")
        await h.engine.decide_approval(mid, "t3", "approve", "tester", "")
        await h.wait_status(mid, "resolved")
        by_task = {a["task_id"]: a["robot_type"] for a in h.store.attempts(mid)}
        # Visual inspection needs a camera, so it routes to the manipulator type.
        assert by_task == {
            "t1": "thermal_drone",
            "t2": "mobile_manipulator",
            "t3": "mobile_manipulator",
            "t4": "thermal_drone",
        }
    finally:
        await close_harness(h)


async def test_freed_robot_unblocks_another_waiting_mission():
    h = await make_harness(fleet=fleet_of(scouts=1, manipulators=1))
    try:
        first = await h.started_mission()
        second = await h.started_mission()
        # The single scout is busy with the first mission, so the second waits for it.
        await h.wait_task(second, "t1", "waiting_resource")
        assert "Inspection quadruped is busy" not in h.task(second, "t1")["status_reason"]
        assert "scout-0000 is busy" in h.task(second, "t1")["status_reason"]
        # When the first mission frees the scout, the second proceeds with no other trigger.
        await h.wait_status(first, "waiting_approval")
        await h.wait_status(second, "waiting_approval")
    finally:
        await close_harness(h)


async def test_large_fleet_runs_many_missions_without_double_booking():
    # 1/20 of real time: at 1/100 a burst of 40 missions outlasts the scaled 150 ms repair
    # timeout, which tests timer compression rather than the engine.
    h = await make_harness(fleet=fleet_of(scouts=500, manipulators=20), time_scale=0.05)
    try:
        started = time.monotonic()
        missions = [await h.started_mission() for _ in range(40)]
        for mid in missions:
            await h.wait_status(mid, "waiting_approval", "no_action_needed", timeout=20)
        for mid in missions:
            if h.mission(mid)["status"] == "waiting_approval":
                await h.engine.decide_approval(mid, "t3", "approve", "tester", "")
        for mid in missions:
            await h.wait_status(mid, "resolved", "no_action_needed", timeout=20)
        elapsed = time.monotonic() - started

        attempts = [a for mid in missions for a in h.store.attempts(mid)]
        # No robot ever ran two attempts at the same time.
        by_robot: dict[str, list[dict]] = {}
        for a in attempts:
            by_robot.setdefault(a["robot_id"], []).append(a)
        for robot_attempts in by_robot.values():
            robot_attempts.sort(key=lambda a: a["started_at"])
            for prev, cur in zip(robot_attempts, robot_attempts[1:]):
                assert prev["ended_at"] <= cur["started_at"]
        # Work spread across the fleet and every attempt kept its robot type.
        assert len(by_robot) > 40
        assert {a["robot_type"] for a in attempts} == {"inspection_quadruped", "mobile_manipulator"}
        assert len(h.commands("repair")) == sum(len(h.store.approvals(m)) for m in missions)
        assert elapsed < 20
    finally:
        await close_harness(h)


async def test_unavailability_reasons_are_summarized_by_type_for_large_fleets():
    h = await make_harness(fleet=fleet_of(scouts=300, manipulators=2))
    try:
        for i in range(300):
            await h.sim.set_online(f"scout-{i:04d}", False)
        await h.wait(lambda: not any(r["online"] for r in h.store.robots() if r["type"] == "inspection_quadruped"))
        mid = await h.started_mission()
        await h.wait_status(mid, "waiting_resource")
        reason = h.task(mid, "t1")["status_reason"]
        assert "Inspection quadruped: 300 offline" in reason
        assert "Mobile manipulator type lacks thermal_inspection" in reason
        assert len(reason) < 200
    finally:
        await close_harness(h)


async def test_many_robot_types_route_only_to_capable_types():
    # 20 types x 50 robots. Capabilities vary by type; each action must land on a capable type.
    capability_sets = [
        ["navigation", "camera"],
        ["navigation", "thermal_inspection"],
        ["navigation", "camera", "thermal_inspection"],
        ["navigation", "camera", "repair"],
        ["camera", "thermal_inspection"],  # cannot travel: never eligible
    ]
    types = {
        f"type-{i:02d}": {"name": f"Type {i:02d}", "capabilities": capability_sets[i % len(capability_sets)]}
        for i in range(20)
    }
    robots = [{"id": f"r-{t}-{j:02d}", "type": t} for t in types for j in range(50)]
    fleet = Fleet.from_dict({"robot_types": types, "robots": robots})
    assert len(fleet.robots) == 1000

    h = await make_harness(fleet=fleet, time_scale=0.05)
    try:
        missions = [await h.started_mission() for _ in range(20)]
        for mid in missions:
            await h.wait_status(mid, "waiting_approval", "no_action_needed", timeout=20)
        for mid in missions:
            if h.mission(mid)["status"] == "waiting_approval":
                await h.engine.decide_approval(mid, "t3", "approve", "tester", "")
        for mid in missions:
            await h.wait_status(mid, "resolved", "no_action_needed", timeout=20)

        from app.catalog import ACTIONS

        attempts = [a for mid in missions for a in h.store.attempts(mid)]
        used_types = set()
        for a in attempts:
            task = h.task(a["mission_id"], a["task_id"])
            assert ACTIONS[task["action"]].required_capabilities <= fleet.types[a["robot_type"]].capabilities
            assert fleet.robots[a["robot_id"]].type.id == a["robot_type"]
            used_types.add(a["robot_type"])
        assert len(used_types) > 1
        assert not any(fleet.types[t].capabilities == frozenset(capability_sets[4]) for t in used_types)
    finally:
        await close_harness(h)
