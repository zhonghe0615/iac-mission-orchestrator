"""HTTP API smoke test through the real FastAPI app and lifespan."""

import time

from fastapi.testclient import TestClient

from app.catalog import DEFAULT_FLEET_FILE
from app.config import Settings
from app.main import create_app

from .conftest import T101, TIME_SCALE


def settings(tmp_path) -> Settings:
    return Settings(
        database_path=str(tmp_path / "api.db"),
        fleet_file=str(DEFAULT_FLEET_FILE),
        sim_time_scale=TIME_SCALE,
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        llm_reasoning_effort="",
        llm_timeout_s=1,
    )


def wait_for_status(client: TestClient, mission_id: str, status: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/missions/{mission_id}").json()
        if detail["mission"]["status"] == status:
            return detail
        time.sleep(0.02)
    raise AssertionError(f"mission did not reach {status}; last: {detail['mission']['status']}")


def test_operator_flow_over_http(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        config = client.get("/api/config").json()
        # Without credentials only the labelled fixture planner is offered.
        assert config["planners"] == [{"mode": "fixture", "label": "fixture planner (deterministic, no LLM)"}]
        assert client.post("/api/missions", json={"work_order": "Inspect T-101", **T101, "planner": "llm"}).status_code == 409

        created = client.post(
            "/api/missions", json={"work_order": "Inspect and resolve the leak on T-101", **T101, "planner": "fixture"}
        )
        assert created.status_code == 201
        mid = created.json()["mission"]["id"]
        wait_for_status(client, mid, "ready")

        bad = client.put(f"/api/missions/{mid}/plan", json={"plan": {"summary": "x", "target": "tank-T101", "tasks": []}})
        assert bad.status_code == 422 and bad.json()["errors"]

        assert client.post(f"/api/missions/{mid}/start", json={"actor": "dana"}).status_code == 200
        detail = wait_for_status(client, mid, "waiting_approval")
        assert any(e["kind"] == "approval_requested" for e in detail["events"])

        early = client.post(f"/api/missions/{mid}/tasks/t4/decision", json={"decision": "approve"})
        assert early.status_code == 409

        ok = client.post(f"/api/missions/{mid}/tasks/t3/decision", json={"decision": "approve", "actor": "dana"})
        assert ok.status_code == 200
        detail = wait_for_status(client, mid, "resolved")
        assert detail["approvals"][0]["actor"] == "dana"

        robots = client.get("/api/robots").json()["robots"]
        assert {r["id"]: r["availability"] for r in robots} == {
            "scout-a": "available",
            "scout-b": "available",
            "manipulator-c": "available",
        }
        armed = client.post("/api/sim/robots/scout-b/fault", json={"fault": "fail_next"}).json()
        assert armed["simulator"]["robots"]["scout-b"]["pending_faults"] == ["fail_next"]
        cleared = client.post("/api/sim/robots/scout-b/fault", json={"fault": "clear"}).json()
        assert cleared["simulator"]["robots"]["scout-b"]["pending_faults"] == []
        assert client.post("/api/sim/robots/scout-a/fault", json={"fault": "offline"}).status_code == 200
        assert client.get("/api/robots").json()["robots"][0]["availability"] == "offline"
        assert client.get("/api/metrics").json()["missions_by_status"] == {"resolved": 1}
