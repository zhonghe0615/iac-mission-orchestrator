"""Plan validation rejects unsupported, malformed, and unsafe plans."""

import copy

import pytest

from app.catalog import Fleet
from app.plan import PlanValidationError, validate_plan
from app.planner import FIXTURE_PLAN


def plan_with(**changes):
    plan = copy.deepcopy(FIXTURE_PLAN)
    for task_id, fields in changes.items():
        task = next(t for t in plan["tasks"] if t["id"] == task_id)
        for key, value in fields.items():
            if value is None:
                task.pop(key, None)
            else:
                task[key] = value
    return plan


FLEET = Fleet.load()


def errors_for(plan, fleet: Fleet = FLEET) -> list[str]:
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, fleet)
    return exc.value.errors


def test_fixture_plan_is_valid():
    assert [t.id for t in validate_plan(FIXTURE_PLAN, FLEET).tasks] == ["t1", "t2", "t3", "t4"]


@pytest.mark.parametrize(
    "plan, expected",
    [
        (plan_with(t1={"action": "weld"}), "unsupported action 'weld'"),
        (plan_with(t3={"args": {"target": "tank-T101"}}), "missing arguments ['procedure']"),
        (plan_with(t3={"args": {"target": "tank-T101", "procedure": "cut_open"}}), "procedure='cut_open'"),
        (plan_with(t1={"args": {"target": "tank-T102"}}), "differs from mission target"),
        (plan_with(t2={"depends_on": ["t9"]}), "unknown task 't9'"),
        (plan_with(t1={"depends_on": ["t4"]}), "dependency cycle"),
        (plan_with(t3={"condition": None}), "conditioned on an inspection finding"),
        (plan_with(t3={"condition": {"task": "t1", "finding": "no_defect"}}), "conditioned on an inspection finding"),
        (plan_with(t4={"condition": {"task": "t3", "finding": "defect_detected"}}), "not reported by action 'repair'"),
        (plan_with(t2={"robot": "scout-a"}), "Extra inputs are not permitted"),
        (plan_with(t3={"requires_approval": False}), "Extra inputs are not permitted"),
    ],
)
def test_invalid_plans_are_rejected(plan, expected):
    assert any(expected in e for e in errors_for(plan)), errors_for(plan)


def test_repair_without_verification_is_rejected():
    plan = copy.deepcopy(FIXTURE_PLAN)
    plan["tasks"] = plan["tasks"][:3]
    assert any("verify_repair" in e for e in errors_for(plan))


def test_repair_without_prior_inspection_is_rejected():
    plan = {
        "summary": "repair straight away",
        "target": "tank-T101",
        "tasks": [
            {"id": "r", "action": "repair", "args": {"target": "tank-T101", "procedure": "seal_weld_seam"}},
            {"id": "v", "action": "verify_repair", "args": {"target": "tank-T101"}, "depends_on": ["r"]},
        ],
    }
    errors = errors_for(plan)
    assert any("must include at least one inspection" in e for e in errors)
    assert any("must depend on an inspection" in e for e in errors)


def test_condition_must_reference_a_dependency():
    plan = plan_with(t2={"depends_on": [], "condition": {"task": "t1", "finding": "defect_detected"}})
    assert any("must be one of its dependencies" in e for e in errors_for(plan))
