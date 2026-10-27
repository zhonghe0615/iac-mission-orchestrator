"""Idempotency of operator requests and robot commands."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.catalog import Fleet
from app.main import create_app
from app.planner import FIXTURE_PLAN, LLMPlanner
from app.store import Store

from .conftest import T101
from .test_api import settings, wait_for_status

WORK_ORDER = {"work_order": "Inspect and resolve the leak on T-101", **T101, "planner": "fixture"}


def test_repeated_create_returns_the_original_mission(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        headers = {"Idempotency-Key": "create-1"}
        first = client.post("/api/missions", json=WORK_ORDER, headers=headers)
        second = client.post("/api/missions", json=WORK_ORDER, headers=headers)
        assert first.status_code == second.status_code == 201
        assert second.headers["Idempotent-Replayed"] == "true"
        assert first.json()["mission"]["id"] == second.json()["mission"]["id"]
        assert len(client.get("/api/missions").json()) == 1

        # Without a key, each request is a new command.
        client.post("/api/missions", json=WORK_ORDER)
        assert len(client.get("/api/missions").json()) == 2


def test_key_reused_with_a_different_body_is_rejected(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        headers = {"Idempotency-Key": "create-2"}
        assert client.post("/api/missions", json=WORK_ORDER, headers=headers).status_code == 201
        other = {**WORK_ORDER, "work_order": "Inspect tank T-102"}
        assert client.post("/api/missions", json=other, headers=headers).status_code == 422
        assert len(client.get("/api/missions").json()) == 1


def test_repeated_approval_is_replayed_not_reapplied(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        mid = client.post("/api/missions", json=WORK_ORDER).json()["mission"]["id"]
        wait_for_status(client, mid, "ready")
        client.post(f"/api/missions/{mid}/start", json={"expected_plan_version": 1})
        detail = wait_for_status(client, mid, "waiting_approval")
        request_seq = next(e["seq"] for e in reversed(detail["events"]) if e["kind"] == "approval_requested")
        url = f"/api/missions/{mid}/tasks/t3/decision"
        headers = {"Idempotency-Key": "approve-1"}
        body = {"decision": "approve", "actor": "dana", "expected_plan_version": 1, "expected_request_seq": request_seq}
        first = client.post(url, json=body, headers=headers)
        second = client.post(url, json=body, headers=headers)
        assert first.status_code == second.status_code == 200
        assert second.headers["Idempotent-Replayed"] == "true"
        # Without the key the second click would have been a 409 conflict.
        assert client.post(url, json={"decision": "approve", "expected_plan_version": 1, "expected_request_seq": request_seq}).status_code == 409
        detail = wait_for_status(client, mid, "resolved")
        assert len(detail["approvals"]) == 1
        assert len([t for t in detail["tasks"] if t["task_id"] == "t3"][0]["attempts"]) == 1


def test_errors_are_stored_and_replayed(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        headers = {"Idempotency-Key": "start-missing"}
        first = client.post("/api/missions/M-missing/start", json={"expected_plan_version": 1}, headers=headers)
        second = client.post("/api/missions/M-missing/start", json={"expected_plan_version": 1}, headers=headers)
        assert first.status_code == second.status_code == 404
        assert second.headers["Idempotent-Replayed"] == "true"


def test_same_key_for_different_endpoints_is_independent(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        headers = {"Idempotency-Key": "shared"}
        mid = client.post("/api/missions", json=WORK_ORDER, headers=headers).json()["mission"]["id"]
        wait_for_status(client, mid, "ready")
        started = client.post(f"/api/missions/{mid}/start", json={"expected_plan_version": 1}, headers=headers)
        assert started.status_code == 200 and "Idempotent-Replayed" not in started.headers


@pytest.mark.anyio
async def test_concurrent_duplicate_is_executed_once(tmp_path):
    # A slow model call keeps the first request in progress while the duplicate arrives.
    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(FIXTURE_PLAN)}}]})

    fleet = Fleet.load()
    planner = LLMPlanner(
        fleet=fleet, base_url="https://llm.invalid/v1", api_key="k", model="m", transport=httpx.MockTransport(slow_handler)
    )
    app = create_app(settings(tmp_path), planners={"llm": planner})
    body = {**WORK_ORDER, "planner": "llm"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Idempotency-Key": "concurrent"}
            first, second = await asyncio.gather(
                client.post("/api/missions", json=body, headers=headers),
                client.post("/api/missions", json=body, headers=headers),
            )
            # The duplicate is either told the first is still running, or gets its stored response.
            codes = sorted([first.status_code, second.status_code])
            assert codes in ([201, 201], [201, 409]), codes
            replay = await client.post("/api/missions", json=body, headers=headers)
            assert replay.status_code == 201 and replay.headers["Idempotent-Replayed"] == "true"
            assert len((await client.get("/api/missions")).json()) == 1

            # Creation returns before the slow planner finishes; the plan arrives later.
            created = replay.json()["mission"]
            assert created["status"] == "planning"
            for _ in range(100):
                status = (await client.get(f"/api/missions/{created['id']}")).json()["mission"]["status"]
                if status == "ready":
                    break
                await asyncio.sleep(0.02)
            assert status == "ready"


def test_unfinished_key_is_released_for_retry(tmp_path):
    store = Store(str(tmp_path / "keys.db"))
    try:
        assert store.claim_idempotency_key("POST /x", "k", "h")[0] == "new"
        assert store.claim_idempotency_key("POST /x", "k", "h")[0] == "in_progress"
        store.release_idempotency_key("POST /x", "k")
        assert store.claim_idempotency_key("POST /x", "k", "h")[0] == "new"
    finally:
        store.close()


@pytest.mark.anyio
async def test_robot_executes_a_repeated_command_once(h):
    mid = await h.started_mission()
    await h.wait(lambda: h.task(mid, "t1")["status"] == "succeeded", what="t1 done")
    command = h.commands("thermal_inspection")[0]

    # Re-send the exact command (as a retrying integration layer might).
    await h.sim.dispatch(command)
    await h.wait(lambda: "duplicate_event_ignored" in h.kinds(mid), what="re-sent result dropped")

    assert len(h.commands("thermal_inspection")) == 1
    assert len([e for e in h.store.evidence(mid) if e["task_id"] == "t1"]) == 1
    assert h.store.task_attempts(mid, "t1")[0]["status"] == "succeeded"
