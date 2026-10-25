"""Planning harness.

The LLM planner sends a bounded site context and action catalog to an
OpenAI-compatible Chat Completions endpoint, validates the returned plan, and
feeds validation errors back for a bounded number of corrections. A failed
model call is reported as a planning failure; it is never replaced with the
fixture plan.
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .catalog import Fleet, site_context
from .plan import Constraints, Plan, PlanValidationError, validate_plan

MAX_RECORDED_OUTPUT = 4000


@dataclass
class PlanningOutcome:
    plan: Plan | None
    error: str | None = None
    calls: list[dict] = field(default_factory=list)


class Planner(Protocol):
    mode: str
    label: str

    async def plan(self, work_order: str, constraints: Constraints) -> PlanningOutcome: ...


FIXTURE_PLAN = {
    "summary": "Thermal then visual inspection of T-101; repair the weld seam only if a defect is confirmed, then verify.",
    "target": "tank-T101",
    "tasks": [
        {
            "id": "t1",
            "action": "thermal_inspection",
            "args": {"target": "tank-T101"},
            "depends_on": [],
            "rationale": "Locate the suspected leak by surface temperature anomaly.",
        },
        {
            "id": "t2",
            "action": "visual_inspection",
            "args": {"target": "tank-T101"},
            "depends_on": ["t1"],
            "rationale": "Capture close-range images of the anomaly as evidence.",
        },
        {
            "id": "t3",
            "action": "repair",
            "args": {"target": "tank-T101", "procedure": "seal_weld_seam"},
            "depends_on": ["t1", "t2"],
            "condition": {"task": "t1", "finding": "defect_detected"},
            "rationale": "Seal the weld seam if the thermal scan confirms a defect.",
        },
        {
            "id": "t4",
            "action": "verify_repair",
            "args": {"target": "tank-T101"},
            "depends_on": ["t3"],
            "rationale": "Confirm the anomaly is gone before closing the work order.",
        },
    ],
}


class FixturePlanner:
    """Deterministic plan for demos and tests. It does not read the work order and is not an LLM.

    It only accepts work orders whose intake fields fit its one plan (T-101, repairs allowed).
    """

    mode = "fixture"
    label = "fixture planner (deterministic, no LLM)"

    def __init__(self, fleet: Fleet):
        self.fleet = fleet

    async def plan(self, work_order: str, constraints: Constraints) -> PlanningOutcome:
        if constraints != Constraints(target=FIXTURE_PLAN["target"], allow_repair=True):
            error = (
                f"the fixture planner only produces its {FIXTURE_PLAN['target']} inspect-and-repair plan; "
                "use the LLM planner for other assets or for missions without repairs"
            )
            return PlanningOutcome(
                plan=None, error=error, calls=[{"summary": f"Fixture planner declined: {error}", "outcome": "declined"}]
            )
        plan = validate_plan(FIXTURE_PLAN, self.fleet, constraints)
        return PlanningOutcome(
            plan=plan,
            calls=[{"summary": "Fixture plan returned; no model was called", "outcome": "fixture", "attempt": 1}],
        )


SYSTEM_PROMPT = """You are the mission planning assistant for an industrial robot orchestration system.
You propose plans. You do not execute anything. A deterministic orchestrator validates your plan,
routes each task to a robot by capability and availability, and enforces operator approval for
high-risk actions. You cannot grant or waive approval.

Return exactly one JSON object and nothing else, with this shape:
{
  "summary": "<one sentence>",
  "target": "<asset id>",
  "tasks": [
    {
      "id": "<short unique id such as t1>",
      "action": "<action name from the catalog>",
      "args": {"<arg name>": "<allowed value>"},
      "depends_on": ["<task id>", ...],
      "condition": {"task": "<id of a dependency>", "finding": "<finding that dependency reports>"} or null,
      "rationale": "<why this step is needed>"
    }
  ]
}

Rules:
- Use only actions, argument names, and argument values listed in the catalog. Every argument is required.
- Every task targets the mission target asset.
- Do not assign robots or robot types; the orchestrator routes each task by capability.
- Do not add fields that are not in the shape above.
- A task's condition may only reference one of its dependencies, using a finding that action reports.
- Include at least one inspection before any repair.
- A repair must depend on an inspection and carry the condition {"task": <inspection id>, "finding": "defect_detected"}.
- Every repair must be followed by a verify_repair task that depends on it.
- Respect the operator constraints given with the work order. They are enforced: a plan for a different
  target, or with a repair when repairs are not allowed, is rejected. If the work order text disagrees
  with them, follow the constraints and say so in the summary.
- Keep the plan minimal: at most 12 tasks."""


def _user_prompt(work_order: str, constraints: Constraints, fleet: Fleet) -> str:
    return (
        "Site context, robot types, and action catalog:\n"
        + json.dumps(site_context(fleet), indent=2)
        + "\n\nOperator constraints (enforced):\n"
        + constraints.describe()
        + "\n\nWork order from the operator:\n"
        + work_order.strip()
    )


def _extract_json(text: str) -> str:
    """Accept a bare object, one in a Markdown code fence, or one surrounded by prose.

    Providers that ignore response_format (such as Anthropic's OpenAI-compatible
    endpoint) may add a sentence around the JSON.
    """
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else text.strip()


class LLMPlanner:
    mode = "llm"

    def __init__(
        self,
        fleet: Fleet,
        base_url: str,
        api_key: str,
        model: str,
        reasoning_effort: str | None = None,
        timeout_s: float = 120.0,
        max_corrections: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.fleet = fleet
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_s = timeout_s
        self.max_corrections = max_corrections
        self.transport = transport
        # The model ID is recorded on every call for audit; the UI shows only "LLM planner".
        self.label = "LLM planner"

    async def _complete(self, client: httpx.AsyncClient, messages: list[dict]) -> tuple[str, dict]:
        body: dict = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        resp = await client.post(
            f"{self.base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        payload = resp.json()
        return payload["choices"][0]["message"]["content"] or "", payload.get("usage") or {}

    async def plan(self, work_order: str, constraints: Constraints) -> PlanningOutcome:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(work_order, constraints, self.fleet)},
        ]
        calls: list[dict] = []
        errors: list[str] = []
        try:
            client = httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport)
        except Exception as exc:  # e.g. an unusable proxy configuration
            calls.append({"attempt": 1, "model": self.model, "outcome": "error", "error": str(exc),
                          "summary": f"Could not create the HTTP client: {exc}"})
            return PlanningOutcome(plan=None, error=f"could not reach the model: {exc}", calls=calls)
        async with client:
            for attempt in range(1, self.max_corrections + 2):
                started = time.monotonic()
                record = {"attempt": attempt, "model": self.model, "reasoning_effort": self.reasoning_effort}
                try:
                    content, usage = await self._complete(client, messages)
                except Exception as exc:  # network, HTTP, or response-shape failure
                    record.update(
                        outcome="error",
                        error=str(exc),
                        latency_ms=round((time.monotonic() - started) * 1000),
                        summary=f"LLM call {attempt} failed: {exc}",
                    )
                    calls.append(record)
                    return PlanningOutcome(plan=None, error=f"model call failed: {exc}", calls=calls)
                record.update(
                    latency_ms=round((time.monotonic() - started) * 1000),
                    usage=usage,
                    output=content[:MAX_RECORDED_OUTPUT],
                )
                try:
                    plan = validate_plan(json.loads(_extract_json(content)), self.fleet, constraints)
                except json.JSONDecodeError as exc:
                    errors = [f"output is not valid JSON: {exc}"]
                except PlanValidationError as exc:
                    errors = exc.errors
                else:
                    record.update(outcome="valid", summary=f"LLM call {attempt} returned a valid plan")
                    calls.append(record)
                    return PlanningOutcome(plan=plan, calls=calls)
                record.update(
                    outcome="invalid",
                    errors=errors,
                    summary=f"LLM call {attempt} returned an invalid plan ({len(errors)} problem(s))",
                )
                calls.append(record)
                messages += [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": "The plan failed validation:\n- "
                        + "\n- ".join(errors)
                        + "\nReturn a corrected JSON object only.",
                    },
                ]
        return PlanningOutcome(
            plan=None,
            error=f"model output failed validation after {len(calls)} attempts: " + "; ".join(errors),
            calls=calls,
        )
