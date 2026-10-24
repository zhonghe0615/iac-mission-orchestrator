"""LLM planning harness against a fake OpenAI-compatible endpoint (no network)."""

import copy
import json

import httpx
import pytest

from app.catalog import Fleet
from app.planner import FIXTURE_PLAN, LLMPlanner

from .conftest import close_harness, make_harness

pytestmark = pytest.mark.anyio


def fake_endpoint(outputs: list, seen: list):
    """Serve the given outputs in order; each is a string (message content) or an int (HTTP status)."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        out = outputs[len(seen) - 1]
        if isinstance(out, int):
            return httpx.Response(out, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": out}}], "usage": {"total_tokens": 1}})

    return httpx.MockTransport(handler)


def planner(outputs: list, seen: list, **kwargs) -> LLMPlanner:
    return LLMPlanner(
        fleet=Fleet.load(),
        base_url="https://llm.invalid/v1",
        api_key="test-key",
        model="test-model",
        transport=fake_endpoint(outputs, seen),
        **kwargs,
    )


async def test_valid_output_is_accepted_and_request_is_structured():
    seen: list = []
    outcome = await planner([json.dumps(FIXTURE_PLAN)], seen, reasoning_effort="high").plan("Inspect T-101")
    assert outcome.plan is not None and outcome.error is None
    request = seen[0]
    assert request["model"] == "test-model"
    assert request["response_format"] == {"type": "json_object"}
    assert request["reasoning_effort"] == "high"
    prompt = request["messages"][1]["content"]
    # The planner sees robot types and counts, never individual robots.
    assert '"type": "inspection_quadruped"' in prompt and '"count": 2' in prompt
    assert "scout-a" not in prompt
    assert [c["outcome"] for c in outcome.calls] == ["valid"]


async def test_invalid_output_gets_bounded_correction():
    unsafe = copy.deepcopy(FIXTURE_PLAN)
    unsafe["tasks"][2].pop("condition")
    seen: list = []
    outcome = await planner(["not json", json.dumps(unsafe), "```json\n" + json.dumps(FIXTURE_PLAN) + "\n```"], seen).plan("x")
    assert outcome.plan is not None
    assert [c["outcome"] for c in outcome.calls] == ["invalid", "invalid", "valid"]
    # Validation errors are fed back to the model.
    assert "conditioned on an inspection finding" in seen[2]["messages"][-1]["content"]


async def test_persistently_invalid_output_fails_without_fallback():
    seen: list = []
    outcome = await planner(["{}", "{}", "{}"], seen).plan("x")
    assert outcome.plan is None
    assert "failed validation after 3 attempts" in outcome.error
    assert len(seen) == 3


async def test_model_call_failure_is_reported():
    outcome = await planner([503], []).plan("x")
    assert outcome.plan is None and "HTTP 503" in outcome.error


async def test_failed_llm_planning_never_substitutes_the_fixture():
    h = await make_harness(planners={"llm": planner([503], [])})
    try:
        mid = await h.engine.create_mission("Inspect and resolve T-101", "llm", "tester")
        mission = h.mission(mid)
        assert mission["status"] == "planning_failed"
        assert mission["plan_version"] == 0 and h.store.plan_versions(mid) == []
        calls = [e for e in h.store.events(mid) if e["kind"] == "planner_call"]
        assert calls[0]["data"]["outcome"] == "error"
        # The operator can still supply a plan manually; it is validated like any other.
        assert await h.engine.update_plan(mid, FIXTURE_PLAN, "tester") == 1
        assert h.store.plan_versions(mid)[0]["source"] == "operator"
    finally:
        await close_harness(h)


async def test_unexpected_planner_error_fails_planning_visibly():
    class BrokenPlanner:
        mode = "llm"
        label = "broken planner"

        async def plan(self, work_order: str):
            raise ImportError("socks support missing")

    h = await make_harness(planners={"llm": BrokenPlanner()})
    try:
        mid = await h.engine.create_mission("Inspect and resolve T-101", "llm", "tester")
        assert h.mission(mid)["status"] == "planning_failed"
        assert "socks support missing" in h.mission(mid)["status_reason"]
    finally:
        await close_harness(h)


async def test_llm_plan_runs_through_the_same_execution_path():
    h = await make_harness(planners={"llm": planner([json.dumps(FIXTURE_PLAN)], [])})
    try:
        mid = await h.engine.create_mission("Inspect and resolve T-101", "llm", "tester")
        assert h.store.plan_versions(mid)[0]["source"] == "llm"
        await h.engine.start_mission(mid, "tester")
        await h.wait_status(mid, "waiting_approval")
        await h.engine.decide_approval(mid, "t3", "approve", "tester", "")
        await h.wait_status(mid, "resolved")
    finally:
        await close_harness(h)
