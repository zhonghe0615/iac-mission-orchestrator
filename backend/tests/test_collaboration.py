"""Stale operator pages cannot overwrite or act on newer mission decisions."""

import asyncio
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.planner import FIXTURE_PLAN

from .conftest import T101
from .test_api import settings, wait_for_status


def create_ready(client: TestClient) -> str:
    body = {"work_order": "Inspect and resolve the leak on T-101", "planner": "fixture", **T101}
    mid = client.post("/api/missions", json=body).json()["mission"]["id"]
    wait_for_status(client, mid, "ready")
    return mid


def test_stale_plan_edit_and_start_are_rejected(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        mid = create_ready(client)
        plan = deepcopy(FIXTURE_PLAN)
        plan["summary"] = "Browser A edit"
        assert client.put(f"/api/missions/{mid}/plan", json={"plan": plan}).status_code == 422
        first = client.put(f"/api/missions/{mid}/plan", json={"plan": plan, "actor": "a", "expected_plan_version": 1})
        assert first.status_code == 200 and first.json()["mission"]["plan_version"] == 2

        plan["summary"] = "Browser B stale edit"
        stale = client.put(f"/api/missions/{mid}/plan", json={"plan": plan, "actor": "b", "expected_plan_version": 1})
        assert stale.status_code == 409 and "plan changed" in stale.json()["detail"]
        assert client.get(f"/api/missions/{mid}").json()["current_plan"]["plan"]["summary"] == "Browser A edit"

        assert client.post(f"/api/missions/{mid}/start", json={"actor": "b"}).status_code == 422
        old_start = client.post(f"/api/missions/{mid}/start", json={"actor": "b", "expected_plan_version": 1})
        assert old_start.status_code == 409 and "plan changed" in old_start.json()["detail"]
        assert client.get(f"/api/missions/{mid}").json()["tasks"] == []

        current = client.post(f"/api/missions/{mid}/start", json={"actor": "a", "expected_plan_version": 2})
        assert current.status_code == 200 and current.json()["mission"]["plan_version"] == 2


def test_two_browser_approval_uses_the_current_request(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        mid = create_ready(client)
        assert client.post(f"/api/missions/{mid}/start", json={"expected_plan_version": 1}).status_code == 200
        detail = wait_for_status(client, mid, "waiting_approval")
        request_seq = next(e["seq"] for e in reversed(detail["events"]) if e["kind"] == "approval_requested")
        url = f"/api/missions/{mid}/tasks/t3/decision"
        basis = {"expected_plan_version": 1, "expected_request_seq": request_seq}
        assert client.post(url, json={"decision": "approve", "expected_plan_version": 1}).status_code == 422

        first = client.post(url, json={"decision": "reject", "actor": "a", **basis})
        stale = client.post(url, json={"decision": "approve", "actor": "b", **basis})
        assert first.status_code == 200
        assert stale.status_code == 409
        final = client.get(f"/api/missions/{mid}").json()
        assert [a["decision"] for a in final["approvals"]] == ["reject"]
        assert not any(t["action"] == "repair" and t["attempts"] for t in final["tasks"])


@pytest.mark.anyio
async def test_simultaneous_edits_and_decisions_have_one_winner(tmp_path):
    app = create_app(settings(tmp_path))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            body = {"work_order": "Inspect and resolve the leak on T-101", "planner": "fixture", **T101}
            mid = (await client.post("/api/missions", json=body)).json()["mission"]["id"]

            async def until(status: str) -> dict:
                for _ in range(200):
                    detail = (await client.get(f"/api/missions/{mid}")).json()
                    if detail["mission"]["status"] == status:
                        return detail
                    await asyncio.sleep(0.01)
                raise AssertionError(f"mission did not reach {status}")

            await until("ready")
            plans = []
            for name in ("A", "B"):
                plan = deepcopy(FIXTURE_PLAN)
                plan["summary"] = name
                plans.append({"plan": plan, "actor": name, "expected_plan_version": 1})
            edits = await asyncio.gather(*(client.put(f"/api/missions/{mid}/plan", json=plan) for plan in plans))
            assert sorted(response.status_code for response in edits) == [200, 409]
            assert (await client.get(f"/api/missions/{mid}")).json()["mission"]["plan_version"] == 2

            assert (await client.post(f"/api/missions/{mid}/start", json={"expected_plan_version": 2})).status_code == 200
            detail = await until("waiting_approval")
            request_seq = next(e["seq"] for e in reversed(detail["events"]) if e["kind"] == "approval_requested")
            basis = {"expected_plan_version": 2, "expected_request_seq": request_seq}
            url = f"/api/missions/{mid}/tasks/t3/decision"
            decisions = await asyncio.gather(
                client.post(url, json={"decision": "approve", "actor": "A", **basis}),
                client.post(url, json={"decision": "reject", "actor": "B", **basis}),
            )
            assert sorted(response.status_code for response in decisions) == [200, 409]
            assert len((await client.get(f"/api/missions/{mid}")).json()["approvals"]) == 1
