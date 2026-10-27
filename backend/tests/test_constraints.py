"""Operator intake fields (target asset, repairs allowed) are enforced on every plan, not left to the planner."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.catalog import Fleet
from app.main import create_app
from app.plan import Constraints, PlanValidationError, validate_plan
from app.planner import FIXTURE_PLAN
from app.store import Store

from .conftest import T101, close_harness, make_harness
from .test_api import settings, wait_for_status
from .test_planner import planner

SURVEY_ORDER = "Do a thermal survey of tank T-102. Do not perform repairs."
T102_NO_REPAIR = {"target": "tank-T102", "allow_repair": False}
T102_SURVEY = {
    "summary": "Thermal survey of T-102; no repairs.",
    "target": "tank-T102",
    "tasks": [{"id": "t1", "action": "thermal_inspection", "args": {"target": "tank-T102"}, "depends_on": []}],
}


def test_plan_for_another_asset_is_rejected():
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(FIXTURE_PLAN, Fleet.load(), Constraints(target="tank-T102", allow_repair=True))
    assert "target: plan targets 'tank-T101' but the operator chose 'tank-T102'" in exc.value.errors


def test_repair_is_rejected_when_repairs_are_not_allowed():
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(FIXTURE_PLAN, Fleet.load(), Constraints(target="tank-T101", allow_repair=False))
    assert "task t3: repair is not allowed; the operator did not allow repairs" in exc.value.errors
    # An inspection-only plan for the chosen asset passes.
    assert validate_plan(T102_SURVEY, Fleet.load(), Constraints(**T102_NO_REPAIR)).target == "tank-T102"


def test_fixture_planner_declines_incompatible_work_orders(tmp_path):
    # The reviewer's case: before, this ran the T-101 repair plan.
    with TestClient(create_app(settings(tmp_path))) as client:
        body = {"work_order": SURVEY_ORDER, "planner": "fixture", **T102_NO_REPAIR}
        mid = client.post("/api/missions", json=body).json()["mission"]["id"]
        detail = wait_for_status(client, mid, "planning_failed")
        assert "fixture planner only produces its tank-T101" in detail["mission"]["status_reason"]
        assert detail["plan_versions"] == []
        assert client.post(f"/api/missions/{mid}/start", json={"expected_plan_version": 1}).status_code == 409

        # T-101 without repairs is declined too: the fixture plan always contains one.
        body = {"work_order": "Inspect T-101, no repairs", "planner": "fixture", "target": "tank-T101", "allow_repair": False}
        mid = client.post("/api/missions", json=body).json()["mission"]["id"]
        wait_for_status(client, mid, "planning_failed")


def test_unknown_target_is_rejected_at_intake(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        body = {"work_order": "Inspect tank T-208", "planner": "fixture", "target": "tank-T208", "allow_repair": True}
        response = client.post("/api/missions", json=body)
        assert response.status_code == 422 and "unknown target asset 'tank-T208'" in response.json()["detail"]
        missing = client.post("/api/missions", json={"work_order": "Inspect tank T-101", "planner": "fixture"})
        assert missing.status_code == 422
        assert client.get("/api/missions").json() == []


@pytest.mark.parametrize(
    ("work_order", "fields", "message"),
    [
        ("Inspect tank T-102", T101, "work order names T-102"),
        ("Inspect T101 and T-102", T101, "work order names multiple target assets"),
        ("Inspect T-101. Do not perform any repairs.", T101, "work order prohibits repairs"),
        ("Inspect T-101. No repairs are allowed.", T101, "work order prohibits repairs"),
        ("Inspect T-101 and repair the seam if needed", {"target": "tank-T101", "allow_repair": False}, "work order requests repair"),
    ],
)
def test_explicit_work_order_conflicts_are_rejected_before_planning(tmp_path, work_order, fields, message):
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.post("/api/missions", json={"work_order": work_order, "planner": "fixture", **fields})
        assert response.status_code == 422
        assert message in response.json()["detail"]
        assert client.get("/api/missions").json() == []


@pytest.mark.parametrize(
    ("work_order", "fields"),
    [
        (SURVEY_ORDER, T102_NO_REPAIR),
        ("Inspect T-101 and report findings only; a repair crew will be scheduled separately.", {"target": "tank-T101", "allow_repair": False}),
        ("Inspect T101 and repair the seam only if a defect is confirmed.", T101),
        ("Inspect T-101; do not repair until approval is granted.", T101),
        ("Inspect T-101; do not repair the seam until approval is granted.", T101),
        ("No repairs have been performed on T-101 yet. Inspect and repair the seam if needed.", T101),
        ("Inspect T-101. No repairs needed based on the last survey; repair if a new defect is found.", T101),
        ("Inspect T-101. No repairs are needed based on the last survey; repair if a new defect is found.", T101),
        ("Inspect T-101. The repair the crew made last week should be verified.", {"target": "tank-T101", "allow_repair": False}),
    ],
)
def test_matching_work_orders_are_accepted_at_intake(tmp_path, work_order, fields):
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.post("/api/missions", json={"work_order": work_order, "planner": "fixture", **fields})
        assert response.status_code == 201


def test_existing_conflicting_mission_cannot_start(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        body = {"work_order": "Inspect and resolve the leak on T-101", "planner": "fixture", **T101}
        mid = client.post("/api/missions", json=body).json()["mission"]["id"]
        wait_for_status(client, mid, "ready")
        client.app.state.store.update_mission(mid, work_order="Inspect T-102. Do not perform repairs.")
        response = client.post(f"/api/missions/{mid}/start", json={"expected_plan_version": 1})
        assert response.status_code == 422
        assert "work order names T-102" in response.json()["detail"]
        detail = client.get(f"/api/missions/{mid}").json()
        assert detail["mission"]["status"] == "ready"
        assert detail["tasks"] == []
        assert "work order names T-102" in detail["intake_conflict"]


def test_old_mission_without_target_shows_a_recovery_action(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        body = {"work_order": "Inspect and resolve the leak on T-101", "planner": "fixture", **T101}
        mid = client.post("/api/missions", json=body).json()["mission"]["id"]
        wait_for_status(client, mid, "ready")
        client.app.state.store.update_mission(mid, target=None)
        detail = client.get(f"/api/missions/{mid}").json()
        assert "no target asset" in detail["intake_conflict"]
        assert client.post(f"/api/missions/{mid}/start", json={"expected_plan_version": 1}).status_code == 422


@pytest.mark.anyio
async def test_llm_is_told_the_constraints_and_corrected_when_it_ignores_them():
    seen: list = []
    # First the model returns the T-101 repair plan; the validation errors send it back for a correction.
    llm = planner([json.dumps(FIXTURE_PLAN), json.dumps(T102_SURVEY)], seen)
    h = await make_harness(planners={"llm": llm})
    try:
        mid = await h.engine.create_mission(SURVEY_ORDER, "llm", "tester", **T102_NO_REPAIR)
        assert h.mission(mid)["status"] == "ready"
        assert "target asset: tank-T102; repairs and other interventions: NOT allowed" in seen[0]["messages"][1]["content"]
        feedback = seen[1]["messages"][-1]["content"]
        assert "operator chose 'tank-T102'" in feedback and "did not allow repairs" in feedback

        # An operator edit cannot reintroduce the repair or change the target either.
        with pytest.raises(PlanValidationError):
            await h.engine.update_plan(mid, FIXTURE_PLAN, "tester")
        await h.engine.start_mission(mid, "tester")
        await h.wait_status(mid, "no_action_needed", "unresolved")
        assert [c.action for c in h.commands()] == ["thermal_inspection"]
        assert h.commands()[0].args["target"] == "tank-T102"
    finally:
        await close_harness(h)


@pytest.mark.anyio
async def test_policy_gate_blocks_a_repair_the_operator_did_not_allow(h):
    # Defense in depth: even if a repair task exists, dispatch re-checks the mission's constraints.
    mid = await h.started_mission()
    await h.wait_status(mid, "waiting_approval")
    h.store.update_mission(mid, allow_repair=0)
    await h.engine.decide_approval(mid, "t3", "approve", "tester", "")
    await h.wait_task(mid, "t3", "needs_intervention")
    assert h.task(mid, "t3")["status_reason"] == "Blocked by policy (repairs_allowed)"
    decision = [e for e in h.store.events(mid) if e["kind"] == "policy_decision" and e["data"]["task_id"] == "t3"][-1]
    assert not decision["data"]["allowed"]
    assert {c["rule"]: c["passed"] for c in decision["data"]["checks"]}["repairs_allowed"] is False
    assert h.commands("repair") == []


def test_existing_database_gains_the_constraint_column(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE missions (id TEXT PRIMARY KEY, work_order TEXT NOT NULL, target TEXT, status TEXT NOT NULL,"
        " status_reason TEXT NOT NULL DEFAULT '', planner_mode TEXT NOT NULL, plan_version INTEGER NOT NULL DEFAULT 0,"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL)"
    )
    conn.execute("INSERT INTO missions VALUES ('M-old', 'x', 'tank-T101', 'resolved', '', 'fixture', 1, 0, 0)")
    conn.commit()
    conn.close()
    store = Store(path)
    try:
        assert store.mission("M-old")["allow_repair"] == 1
    finally:
        store.close()
