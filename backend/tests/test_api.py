from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from app.main import app
from app import main as app_main


def test_api_exposes_state_and_read_only_tools(monkeypatch):
    # API contract tests must not depend on the external Ollama residency state;
    # the real preload lifecycle has dedicated unit and smoke coverage.
    monkeypatch.setattr(app_main.settings, "ollama_preload", False)
    with TestClient(app) as client:
        state = client.get("/api/state")
        tools = client.get("/api/tools")
        health = client.get("/api/health")
        listening = client.get("/api/listening/settings")
        network = client.get("/api/network-watch/status")
        assert state.status_code == 200
        assert state.json()["state"] in {"neutral", "happy", "curious", "focused", "concerned", "amused", "tired", "surprised"}
        assert tools.status_code == 200
        assert {item["name"] for item in tools.json()} >= {"ping_host", "get_local_system_stats", "get_network_status", "get_network_metrics", "get_recent_network_events", "get_network_quality_summary"}
        assert "system_shell" in {item["name"] for item in tools.json()}
        assert "remote_shell" in {item["name"] for item in tools.json()}

        remote = client.get("/api/remote-shell/status")
        assert remote.status_code == 200 and remote.json()["enabled"] is True
        assert {item["id"] for item in remote.json()["hosts"]} >= {"gateway", "proxmox", "dc1"}

        agent = client.get("/api/agent/status")
        assert agent.status_code == 200 and agent.json()["max_tool_calls"] == 20
        assert client.get("/api/shell/status").json()["enabled"] is True
        assert health.json().keys() >= {"always_listening", "microphone", "wake_word", "network_watch"}
        assert isinstance(health.json()["llm_ready"], bool)
        assert listening.status_code == 200 and isinstance(listening.json()["settings"]["enabled"], bool)
        assert network.status_code == 200 and isinstance(network.json()["enabled"], bool)


def test_chat_fails_fast_while_model_is_warming(monkeypatch):
    monkeypatch.setattr(app_main.settings, "ollama_preload", False)
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.services.llm, "ready", AsyncMock(return_value=False))
        response = client.post("/api/chat", json={"message": "Olá", "synthesize": False})
        assert response.status_code == 503
        assert "inicializando" in response.json()["detail"]


def test_runtime_supervisor_endpoints_expose_registered_services_only(monkeypatch):
    monkeypatch.setattr(app_main.settings, "ollama_preload", False)
    with TestClient(app) as client:
        tools = {item["name"] for item in client.get("/api/tools").json()}
        assert {"runtime_status", "runtime_start", "runtime_stop", "runtime_restart", "runtime_health", "runtime_logs"} <= tools

        listing = client.get("/api/runtime/services")
        assert listing.status_code == 200
        services = {item["id"]: item for item in listing.json()["services"]}
        assert {"kazumi_backend", "kazumi_frontend_dev", "ollama", "utamo_sentinel", "kazumi_test_service"} <= set(services)
        assert all(item["validation_error"] is None for item in services.values())

        assert client.get("/api/runtime/services/inexistente").status_code == 404
        assert client.get("/api/runtime/services/kazumi_backend/health").status_code == 200
        logs = client.get("/api/runtime/services/kazumi_backend/logs?lines=5")
        assert logs.status_code == 200 and logs.json()["success"] is True

        blocked = client.post("/api/runtime/services/kazumi_backend/restart", json={}).json()
        assert blocked["error_code"] == "SELF_RESTART_UNSUPPORTED"

        history = client.get("/api/runtime/history")
        assert history.status_code == 200 and isinstance(history.json()["events"], list)
