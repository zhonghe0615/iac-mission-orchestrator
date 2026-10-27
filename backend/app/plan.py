"""Mission plan schema and validation.

Model-generated and operator-edited plans pass through the same checks.
Approval is deliberately not part of the plan: whether an action needs
approval comes from the action catalog and is enforced by the engine.
"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .catalog import ACTIONS, TARGETS, Fleet, actions_with_role

MAX_TASKS = 12


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str
    finding: str


class PlanTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    action: str
    args: dict[str, str]
    depends_on: list[str] = Field(default_factory=list)
    condition: Condition | None = None
    rationale: str = ""


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    target: str
    tasks: list[PlanTask] = Field(min_length=1, max_length=MAX_TASKS)


@dataclass(frozen=True)
class Constraints:
    """Operator intake fields that every plan for the mission must respect.

    The plan check uses explicit fields. A separate intake check rejects clear
    contradictions between these fields and the work order text.
    """

    target: str
    allow_repair: bool

    def describe(self) -> str:
        repairs = "allowed (only if a defect is confirmed)" if self.allow_repair else "NOT allowed"
        return f"target asset: {self.target}; repairs and other interventions: {repairs}"


class PlanValidationError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _format_pydantic(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "plan"
        out.append(f"{loc}: {err['msg']}")
    return out


def _ancestors(tasks: dict[str, PlanTask]) -> dict[str, set[str]]:
    """Transitive dependencies of each task. Assumes the graph is acyclic."""
    memo: dict[str, set[str]] = {}

    def visit(tid: str) -> set[str]:
        if tid not in memo:
            result: set[str] = set()
            for dep in tasks[tid].depends_on:
                result.add(dep)
                result |= visit(dep)
            memo[tid] = result
        return memo[tid]

    for tid in tasks:
        visit(tid)
    return memo


def _find_cycle(tasks: dict[str, PlanTask]) -> list[str] | None:
    state: dict[str, int] = {}  # 1 = visiting, 2 = done
    stack: list[str] = []

    def visit(tid: str) -> list[str] | None:
        state[tid] = 1
        stack.append(tid)
        for dep in tasks[tid].depends_on:
            if state.get(dep) == 1:
                return stack[stack.index(dep):] + [dep]
            if state.get(dep) is None:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        state[tid] = 2
        return None

    for tid in tasks:
        if state.get(tid) is None:
            found = visit(tid)
            if found:
                return found
    return None


def validate_plan(data: Any, fleet: Fleet, constraints: Constraints | None = None) -> Plan:
    """Return a validated plan or raise PlanValidationError with every problem found.

    With constraints, the plan must also target the operator's chosen asset and,
    if repairs are not allowed, contain no intervention.
    """
    try:
        plan = Plan.model_validate(data)
    except ValidationError as exc:
        raise PlanValidationError(_format_pydantic(exc)) from None

    errors: list[str] = []
    if plan.target not in TARGETS:
        errors.append(f"target: unknown asset '{plan.target}'; known assets: {', '.join(TARGETS)}")
    if constraints and plan.target != constraints.target:
        errors.append(f"target: plan targets '{plan.target}' but the operator chose '{constraints.target}'")

    tasks: dict[str, PlanTask] = {}
    for t in plan.tasks:
        if t.id in tasks:
            errors.append(f"task {t.id}: duplicate task id")
        tasks[t.id] = t

    for t in plan.tasks:
        spec = ACTIONS.get(t.action)
        if spec is None:
            errors.append(f"task {t.id}: unsupported action '{t.action}'; supported: {', '.join(ACTIONS)}")
            continue
        missing = set(spec.args) - set(t.args)
        unexpected = set(t.args) - set(spec.args)
        if missing:
            errors.append(f"task {t.id}: missing arguments {sorted(missing)}")
        if unexpected:
            errors.append(f"task {t.id}: unexpected arguments {sorted(unexpected)}")
        for name, value in t.args.items():
            allowed = spec.args.get(name)
            if allowed is not None and value not in allowed:
                errors.append(f"task {t.id}: argument {name}='{value}' not in {list(allowed)}")
        if "target" in t.args and t.args["target"] != plan.target:
            errors.append(f"task {t.id}: target '{t.args['target']}' differs from mission target '{plan.target}'")
        if constraints and not constraints.allow_repair and spec.role == "intervention":
            errors.append(f"task {t.id}: {t.action} is not allowed; the operator did not allow repairs")
        if not fleet.capable(t.action):
            errors.append(f"task {t.id}: no robot in the fleet has capabilities {sorted(spec.required_capabilities)}")
        for dep in t.depends_on:
            if dep == t.id:
                errors.append(f"task {t.id}: depends on itself")
            elif dep not in tasks:
                errors.append(f"task {t.id}: depends on unknown task '{dep}'")

    # Graph checks need a well-formed graph.
    if errors:
        raise PlanValidationError(errors)

    cycle = _find_cycle(tasks)
    if cycle:
        raise PlanValidationError([f"dependency cycle: {' -> '.join(cycle)}"])

    ancestors = _ancestors(tasks)

    for t in plan.tasks:
        if t.condition is None:
            continue
        src = tasks.get(t.condition.task)
        if src is None:
            errors.append(f"task {t.id}: condition references unknown task '{t.condition.task}'")
            continue
        if src.id not in ancestors[t.id]:
            errors.append(f"task {t.id}: condition task '{src.id}' must be one of its dependencies")
        if t.condition.finding not in ACTIONS[src.action].findings:
            errors.append(
                f"task {t.id}: finding '{t.condition.finding}' is not reported by action '{src.action}'"
                f" (reports {list(ACTIONS[src.action].findings)})"
            )

    inspections = actions_with_role("inspection")
    if not any(t.action in inspections for t in plan.tasks):
        errors.append("plan: must include at least one inspection task")

    # Safety sequencing for actions that change the physical asset, driven by action roles.
    for t in plan.tasks:
        role = ACTIONS[t.action].role
        if role == "intervention":
            if not any(tasks[a].action in inspections for a in ancestors[t.id]):
                errors.append(f"task {t.id}: {t.action} must depend on an inspection of the target")
            cond = t.condition
            src = tasks.get(cond.task) if cond else None
            if src is None or src.action not in inspections or cond.finding != ACTIONS[src.action].defect_finding:
                errors.append(
                    f"task {t.id}: {t.action} must be conditioned on an inspection finding 'defect_detected'"
                )
            verifiers = [
                v
                for v in plan.tasks
                if t.action in ACTIONS[v.action].verifies and t.id in ancestors[v.id]
            ]
            if not verifiers:
                names = [a for a, spec in ACTIONS.items() if t.action in spec.verifies]
                errors.append(f"task {t.id}: {t.action} must be followed by a {' or '.join(names)} task that depends on it")
        elif role == "verification":
            verified = ACTIONS[t.action].verifies
            if not any(tasks[a].action in verified for a in ancestors[t.id]):
                errors.append(f"task {t.id}: {t.action} must depend on a {' or '.join(verified)} task")

    if errors:
        raise PlanValidationError(errors)
    return plan
