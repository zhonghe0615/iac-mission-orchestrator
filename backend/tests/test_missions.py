"""Behavioral tests for the required mission scenarios and execution invariants."""

import pytest

from app.engine import EngineError
from app.plan import PlanValidationError
from app.planner import FIXTURE_PLAN

pytestmark = pytest.mark.anyio


async def test_approved_repair_resolves_mission(h):
    mid = await h.started_mission()
    await h.wait_status(mid, "waiting_approval")

    # Inspection ran first and produced evidence; nothing was repaired yet.
    assert h.task(mid, "t1")["finding"] == "defect_detected"
    assert [c.action for c in h.commands()] == ["thermal_inspection", "visual_inspection"]
    assert {e["task_id"] for e in h.store.evidence(mid)} == {"t1", "t2"}
    assert h.task(mid, "t3")["status"] == "awaiting_approval"

    await h.engine.decide_approval(mid, "t3", "approve", "tester", "confirmed by thermal scan")
    await h.wait_status(mid, "resolved")

    repairs = h.commands("repair")
    assert len(repairs) == 1 and repairs[0].robot_id == "manipulator-c"
    assert h.task(mid, "t4")["finding"] == "repair_verified"
    approvals = h.store.approvals(mid)
    assert len(approvals) == 1 and approvals[0]["consumed_by"] == h.store.task_attempts(mid, "t3")[0]["id"]
    assert "mission_closed" in h.kinds(mid)


async def test_denied_repair_dispatches_no_repair(h):
    mid = await h.started_mission()
    await h.wait_status(mid, "waiting_approval")

    await h.engine.decide_approval(mid, "t3", "reject", "tester", "schedule a shutdown first")
    await h.wait_status(mid, "unresolved")

    assert h.commands("repair") == []
    assert h.task(mid, "t3")["status"] == "rejected"
    assert h.task(mid, "t4")["status"] == "skipped"
    assert "remains open" in h.mission(mid)["status_reason"]
    assert h.sim.assets["tank-T101"].repaired is False


async def test_scout_a_offline_during_inspection_reassigns_to_b(h):
    mid = await h.started_mission()
    await h.wait(lambda: h.commands("thermal_inspection"), what="thermal dispatch")
    assert h.commands("thermal_inspection")[0].robot_id == "scout-a"

    await h.sim.set_online("scout-a", False)
    await h.wait_status(mid, "waiting_approval")

    attempts = h.store.task_attempts(mid, "t1")
    assert [(a["robot_id"], a["status"]) for a in attempts] == [("scout-a", "lost"), ("scout-b", "succeeded")]
    # The same mission continued; evidence comes from the successful attempt only.
    t1_evidence = [e for e in h.store.evidence(mid) if e["task_id"] == "t1"]
    assert [e["attempt_id"] for e in t1_evidence] == [attempts[1]["id"]]
    assert len(h.store.missions()) == 1


async def test_no_replacement_waits_with_reason_then_resumes(h):
    await h.sim.set_online("scout-b", False)
    mid = await h.started_mission()
    await h.wait(lambda: h.commands("thermal_inspection"), what="thermal dispatch")
    await h.sim.set_online("scout-a", False)

    await h.wait_status(mid, "waiting_resource")
    # Under load the 30 ms test lease can expire before the announced offline event is
    # processed, briefly showing "unreachable"; wait for the settled reason.
    await h.wait(
        lambda: "Scout A is offline" in h.task(mid, "t1")["status_reason"]
        and "Scout B is offline" in h.task(mid, "t1")["status_reason"],
        what="both scouts reported offline",
    )
    reason = h.task(mid, "t1")["status_reason"]
    assert "Mobile manipulator type lacks thermal_inspection" in reason
    assert h.mission(mid)["status"] not in ("resolved", "no_action_needed")

    await h.sim.set_online("scout-b", True)
    await h.wait_status(mid, "waiting_approval")
    assert h.store.task_attempts(mid, "t1")[-1]["robot_id"] == "scout-b"


async def test_late_result_from_timed_out_attempt_is_ignored(h):
    h.sim.arm_fault("scout-a", "hang_next")
    mid = await h.started_mission()
    await h.wait_status(mid, "waiting_approval")

    attempts = h.store.task_attempts(mid, "t1")
    assert [(a["robot_id"], a["status"]) for a in attempts] == [("scout-a", "timed_out"), ("scout-b", "succeeded")]

    # Scout A's late result arrives after reassignment and must not change state.
    await h.wait(lambda: "stale_result_ignored" in h.kinds(mid), what="stale result")
    assert h.store.task_attempts(mid, "t1")[0]["status"] == "timed_out"
    assert h.task(mid, "t1")["status"] == "succeeded"
    t1_evidence = [e for e in h.store.evidence(mid) if e["task_id"] == "t1"]
    assert [e["robot_id"] for e in t1_evidence] == ["scout-b"]
    # Responding again clears the unresponsive mark.
    await h.wait(lambda: h.store.robot("scout-a")["health"] == "ok", what="scout-a healthy")


async def test_unknown_repair_outcome_is_not_redispatched(h):
    mid = await h.started_mission()
    await h.wait_status(mid, "waiting_approval")
    await h.engine.decide_approval(mid, "t3", "approve", "tester", "")
    await h.wait(lambda: h.commands("repair"), what="repair dispatch")

    await h.sim.set_online("manipulator-c", False)
    await h.wait_status(mid, "needs_intervention")
    await h.sim.set_online("manipulator-c", True)
    await h.wait(lambda: h.store.robot("manipulator-c")["online"], what="C online")

    assert h.task(mid, "t3")["status"] == "needs_intervention"
    assert len(h.commands("repair")) == 1

    # An operator retry needs a fresh approval; the first one was consumed.
    await h.engine.retry_task(mid, "t3", "tester")
    await h.wait_task(mid, "t3", "awaiting_approval")
    assert len(h.commands("repair")) == 1
    await h.engine.decide_approval(mid, "t3", "approve", "tester", "site confirmed no repair applied")
    await h.wait_status(mid, "resolved")
    assert len(h.commands("repair")) == 2


async def test_api_requests_cannot_bypass_approval(h):
    mid = await h.started_mission()
    with pytest.raises(EngineError):
        await h.engine.decide_approval(mid, "t1", "approve", "tester", "")  # not an approval point
    await h.wait_status(mid, "waiting_approval")
    with pytest.raises(EngineError):
        await h.engine.retry_task(mid, "t3", "tester")
    with pytest.raises(EngineError):
        await h.engine.update_plan(mid, FIXTURE_PLAN, "tester")  # plan is frozen after start
    assert h.commands("repair") == []

    await h.engine.decide_approval(mid, "t3", "reject", "tester", "")
    with pytest.raises(EngineError):
        await h.engine.decide_approval(mid, "t3", "approve", "tester", "")  # no reversal after rejection
    assert h.commands("repair") == []


async def test_edited_plan_is_revalidated_before_start(h):
    mid = await h.engine.create_mission("Inspect T-101", "fixture", "tester")
    unsafe = {**FIXTURE_PLAN, "tasks": [t for t in FIXTURE_PLAN["tasks"] if t["id"] != "t4"]}
    with pytest.raises(PlanValidationError) as exc:
        await h.engine.update_plan(mid, unsafe, "tester")
    assert any("verify_repair" in e for e in exc.value.errors)
    assert h.mission(mid)["plan_version"] == 1

    inspection_only = {**FIXTURE_PLAN, "tasks": FIXTURE_PLAN["tasks"][:1]}
    assert await h.engine.update_plan(mid, inspection_only, "tester") == 2
    await h.engine.start_mission(mid, "tester")
    await h.wait_status(mid, "unresolved")
    assert "no verified repair" in h.mission(mid)["status_reason"]


async def test_no_defect_skips_repair(h):
    h.sim.set_defect("tank-T101", False)
    mid = await h.started_mission()
    await h.wait_status(mid, "no_action_needed")
    assert h.task(mid, "t3")["status"] == "skipped"
    assert h.task(mid, "t4")["status"] == "skipped"
    assert h.commands("repair") == []


async def test_inspection_failure_escalates_after_retry_budget(h):
    for robot in ("scout-a", "scout-b"):
        h.sim.arm_fault(robot, "fail_next")
    await h.sim.set_online("manipulator-c", False)
    mid = await h.started_mission()
    # A fails, B fails, then A again succeeds: two retries within the budget of three.
    await h.wait_status(mid, "waiting_approval")
    attempts = h.store.task_attempts(mid, "t1")
    assert [(a["robot_id"], a["status"]) for a in attempts] == [
        ("scout-a", "failed"),
        ("scout-b", "failed"),
        ("scout-a", "succeeded"),
    ]


async def test_cancel_keeps_reservation_until_robot_confirms(h):
    mid = await h.started_mission()
    await h.wait(lambda: h.commands("thermal_inspection"), what="thermal dispatch")
    await h.engine.cancel_mission(mid, "tester")
    assert h.mission(mid)["status"] == "cancelled"
    await h.wait(
        lambda: h.store.task_attempts(mid, "t1")[0]["status"] == "cancelled", what="cancel confirmation"
    )
    assert h.store.robot("scout-a")["reserved_attempt"] is None


async def test_independent_inspections_run_in_parallel_on_a_and_b(h):
    # Thermal (t1) and visual (t2) have no dependency on each other; the repair waits for both.
    parallel = {**FIXTURE_PLAN, "tasks": [dict(t) for t in FIXTURE_PLAN["tasks"]]}
    parallel["tasks"][1]["depends_on"] = []
    mid = await h.engine.create_mission("Inspect and resolve the suspected leak on tank T-101", "fixture", "tester")
    await h.engine.update_plan(mid, parallel, "tester")
    await h.engine.start_mission(mid, "tester")

    # Both inspections are running at the same moment, each on its own Scout.
    await h.wait(
        lambda: h.task(mid, "t1")["status"] == "running" and h.task(mid, "t2")["status"] == "running",
        what="t1 and t2 running together",
    )
    await h.wait_status(mid, "waiting_approval")

    (thermal,) = h.store.task_attempts(mid, "t1")
    (visual,) = h.store.task_attempts(mid, "t2")
    assert {thermal["robot_id"], visual["robot_id"]} == {"scout-a", "scout-b"}
    # The recorded attempt intervals overlap.
    assert thermal["started_at"] < visual["ended_at"] and visual["started_at"] < thermal["ended_at"]
    # The repair was not requested until both inspections had finished.
    assert h.task(mid, "t3")["status"] == "awaiting_approval"
    assert {e["task_id"] for e in h.store.evidence(mid)} == {"t1", "t2"}
