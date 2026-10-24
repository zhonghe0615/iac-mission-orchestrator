"""Bonus reliability behavior: duplicate/out-of-order events, restart recovery, metrics."""

import pytest

from app.metrics import compute_metrics

from .conftest import close_harness, make_harness

pytestmark = pytest.mark.anyio


async def test_duplicate_completion_does_not_repeat_transitions(h):
    h.sim.arm_fault("scout-a", "duplicate_next")
    mid = await h.started_mission()
    await h.wait_status(mid, "waiting_approval")
    await h.wait(lambda: "duplicate_event_ignored" in h.kinds(mid), what="duplicate ignored")
    assert h.kinds(mid).count("task_succeeded") == 2  # t1 and t2 once each
    assert len([e for e in h.store.evidence(mid) if e["task_id"] == "t1"]) == 1


async def test_out_of_order_accepted_after_completion_is_ignored(h):
    h.sim.arm_fault("scout-a", "reorder_next")
    mid = await h.started_mission()
    await h.wait_status(mid, "waiting_approval")
    await h.wait(lambda: "stale_event_ignored" in h.kinds(mid), what="reordered event ignored")
    assert h.store.task_attempts(mid, "t1")[0]["status"] == "succeeded"


async def test_restart_while_waiting_for_approval_recovers_decision_context(tmp_path):
    db = str(tmp_path / "orchestrator.db")
    first = await make_harness(db)
    mid = await first.started_mission()
    await first.wait_status(mid, "waiting_approval")
    await close_harness(first)

    second = await make_harness(db)
    try:
        assert second.mission(mid)["status"] == "waiting_approval"
        assert second.task(mid, "t3")["status"] == "awaiting_approval"
        assert len(second.store.evidence(mid)) == 2
        await second.engine.decide_approval(mid, "t3", "approve", "tester", "after restart")
        await second.wait_status(mid, "resolved")
    finally:
        await close_harness(second)


async def test_restart_with_open_inspection_redispatches_safely(tmp_path):
    db = str(tmp_path / "orchestrator.db")
    first = await make_harness(db)
    mid = await first.started_mission()
    await first.wait(lambda: first.commands("thermal_inspection"), what="thermal dispatch")
    await close_harness(first)

    second = await make_harness(db)
    try:
        await second.wait_status(mid, "waiting_approval")
        attempts = second.store.task_attempts(mid, "t1")
        assert attempts[0]["status"] == "lost"
        assert attempts[-1]["status"] == "succeeded"
    finally:
        await close_harness(second)


async def test_restart_with_open_repair_requires_intervention(tmp_path):
    db = str(tmp_path / "orchestrator.db")
    first = await make_harness(db)
    mid = await first.started_mission()
    await first.wait_status(mid, "waiting_approval")
    await first.engine.decide_approval(mid, "t3", "approve", "tester", "")
    await first.wait(lambda: first.commands("repair"), what="repair dispatch")
    await close_harness(first)

    second = await make_harness(db)
    try:
        await second.wait_status(mid, "needs_intervention")
        assert second.commands("repair") == []  # not redispatched by the new process
        assert second.task(mid, "t3")["status"] == "needs_intervention"
    finally:
        await close_harness(second)


async def test_restart_during_planning_fails_visibly(tmp_path):
    db = str(tmp_path / "orchestrator.db")
    first = await make_harness(db)
    with first.store.tx():
        first.store.insert_mission(
            {"id": "M-plan", "work_order": "x", "status": "planning", "planner_mode": "llm", "created_at": 0, "updated_at": 0}
        )
    await close_harness(first)

    second = await make_harness(db)
    try:
        assert second.mission("M-plan")["status"] == "planning_failed"
    finally:
        await close_harness(second)


async def test_metrics_reflect_recovery(h):
    mid = await h.started_mission()
    await h.wait(lambda: h.commands("thermal_inspection"), what="thermal dispatch")
    await h.sim.set_online("scout-a", False)
    await h.wait_status(mid, "waiting_approval")
    await h.engine.decide_approval(mid, "t3", "approve", "tester", "")
    await h.wait_status(mid, "resolved")

    m = compute_metrics(h.store)
    assert m["missions_by_status"] == {"resolved": 1}
    assert m["completion_rate"] == 1.0
    assert m["retries"] == 1 and m["reassignments"] == 1
    assert m["mean_time_to_recover_s"] is not None
    assert m["operator_actions"] == 1
