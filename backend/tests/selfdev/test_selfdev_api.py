from fastapi.testclient import TestClient

from app.main import app


def test_selfdev_api_exposes_real_state_and_blocks_protected_change() -> None:
    with TestClient(app) as client:
        status = client.get("/api/selfdev/status")
        assert status.status_code == 200
        assert status.json()["mode"] in {"OFF", "OBSERVE_ONLY", "AUTONOMOUS_SAFE", "AUTONOMOUS_ADVANCED"}

        created = client.post("/api/selfdev/issues", json={
            "title": "Revisar fluxo de aprovação",
            "description": "Solicitação explícita para validar que áreas protegidas não são promovidas automaticamente.",
            "components": ["backend/app/tools/approval.py"],
        })
        assert created.status_code == 200
        issue_id = created.json()["issue_id"]

        listed = client.get("/api/selfdev/issues")
        assert listed.status_code == 200
        assert any(item["issue_id"] == issue_id for item in listed.json()["issues"])
        assert client.get(f"/api/selfdev/issues/{issue_id}").status_code == 200

        run = client.post("/api/selfdev/run-once", json={"issue_id": issue_id})
        assert run.status_code == 200
        assert run.json()["status"] == "BLOCKED"
        assert run.json()["error_code"] == "RISK_NOT_AUTOPROMOTABLE"

        notifications = client.get("/api/selfdev/notifications")
        assert notifications.status_code == 200
        assert notifications.json()["unread"] >= 1
        notification_id = notifications.json()["notifications"][0]["notification_id"]
        assert client.post(f"/api/selfdev/notifications/{notification_id}/read").json()["success"] is True

        query = client.get("/api/selfdev/repository/query", params={"q": "onde está SelfDevelopmentService"})
        assert query.status_code == 200
        assert query.json()["kind"] == "symbol"
