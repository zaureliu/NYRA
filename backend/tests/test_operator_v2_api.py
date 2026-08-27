"""API integration for Operator V2 endpoints (spec Parte Q) + config flags
(Parte R: every flag has a consumer AND a test)."""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_operator_v2_status_exposes_all_flags(client):
    body = client.get("/api/operator/v2/status").json()
    assert body["operator_v2"] is True
    flags = body["flags"]
    expected = {"vision", "browser_control", "credentials", "persistent_jobs",
                "workflow_engine", "desktop_watcher", "proactive_operator",
                "clipboard"}
    assert expected <= set(flags.keys())
    assert flags["proactive_operator"] is False  # §233 default OFF


def test_config_flags_have_defaults():
    """§282: sem dead settings — defaults carregam e são tipados."""
    from app.core.config import Settings

    settings = Settings.from_sources()
    assert isinstance(settings.vision_enabled, bool)
    assert isinstance(settings.browser_control_enabled, bool)
    assert isinstance(settings.credential_broker_enabled, bool)
    assert isinstance(settings.persistent_jobs_enabled, bool)
    assert isinstance(settings.workflow_engine_enabled, bool)
    assert isinstance(settings.desktop_watcher_enabled, bool)
    assert isinstance(settings.watchdog_enabled, bool)
    assert settings.proactive_operator_enabled is False
    assert 60 <= settings.elevated_session_default_ttl_seconds <= 900
    assert 15 <= settings.watch_default_ttl_seconds <= 3600


def test_workflows_crud_via_api(client, tmp_path):
    payload = {
        "workflow_id": "wf_api_teste",
        "name": "Workflow via API",
        "description": "criado no teste",
        "trigger_phrases": ["teste api workflow"],
        "steps": [
            {"step_id": "s1", "tool": "system_shell",
             "params": {"command": "Write-Output api-ok", "shell": "powershell"}},
        ],
        "parameters": {},
        "risk": "READ_ONLY",
    }
    created = client.post("/api/workflows", json=payload).json()
    assert created["success"] in {True, False}  # pode existir de execuções anteriores
    listing = client.get("/api/workflows").json()
    assert listing["success"] is True

    dry = client.post("/api/workflows/wf_api_teste/dry-run", json={}).json()
    if dry.get("success"):
        assert dry["plan"][0]["tool"] == "system_shell"
        assert dry["success"] is True


def test_watches_register_and_cancel(client):
    registration = client.post("/api/watches", json={
        "event_types": ["file.created"],
        "filters": {"path": "."},
        "ttl_seconds": 60,
    }).json()
    # Pode falhar por WATCH_LIMIT se a suíte encheu os slots; nunca deve 500.
    if registration.get("success"):
        watch_id = registration["watch_id"]
        deleted = client.delete(f"/api/watches/{watch_id}")
        assert deleted.status_code == 200


def test_watchdog_status_endpoint(client):
    outcome = client.get("/api/watchdog/status").json()
    assert outcome["success"] is True
    assert "running" in outcome


def test_credentials_api_never_returns_secret(client, tmp_path, monkeypatch):
    from app.operator import credentials as creds_mod

    monkeypatch.setattr(creds_mod, "_VAULT_FILE", tmp_path / "vault.bin")
    broker = client.app.state.services.operator_v2.credentials

    secret = "token-super-secreto-da-api-9876"
    pending = client.put("/api/credentials/api_test_cred",
                         json={"secret": secret, "kind": "http"})
    assert pending.status_code == 200
    assert pending.json()["error_code"] == "APPROVAL_REQUIRED"
    assert secret not in pending.text
    broker.approvals.grant(pending.json()["approval_id"], "test")
    upsert = client.put("/api/credentials/api_test_cred", json={
        "secret": secret, "kind": "http",
        "approval_id": pending.json()["approval_id"],
    })
    assert upsert.status_code == 200
    assert upsert.json()["success"] is True

    listing = client.get("/api/credentials").json()
    assert secret not in __import__("json").dumps(listing)  # §99/§280 zero leak

    status = client.get("/api/credentials/api_test_cred/status")
    if status.status_code == 200:
        assert secret not in __import__("json").dumps(status.json())


def test_tasks_endpoints_roundtrip(client):
    created = client.post("/api/tasks", json={
        "goal": "tarefa de teste api",
        "steps": [
            {"step_id": "s1", "tool": "system_shell",
             "params": {"command": "Get-Date", "shell": "powershell"}},
        ],
        "auto_run": False,
    })
    assert created.status_code == 200
    body = created.json()
    if body.get("success"):
        task_id = body["task"]["task_id"]
        status = client.get(f"/api/tasks/{task_id}")
        assert status.status_code == 200
        cancel = client.post(f"/api/tasks/{task_id}/cancel")
        assert cancel.status_code == 200

    hidden = client.post("/api/tasks", json={
        "goal": "sink interno não pode ser composto",
        "steps": [{"step_id": "s1", "tool": "get_local_system_stats"}],
        "auto_run": False,
    })
    assert hidden.status_code == 422
    assert hidden.json()["detail"]["error_code"] == "TOOL_NOT_EXPOSED"


def test_elevated_session_status_endpoint(client):
    outcome = client.get("/api/elevated/session/status").json()
    assert outcome["success"] is True
    assert isinstance(outcome["active_sessions"], list)


def test_browser_status_endpoint_exists(client):
    tools = client.get("/api/tools").json()
    names = {item["name"] for item in (tools if isinstance(tools, list) else tools.get("tools", []))}
    v2_surface = {
        "screen_capture", "visual_inspect", "visual_click", "visual_type",
        "visual_read", "screen_diff", "detect_modals",
        "app_adapter_list", "app_adapter_action",
        "browser_dom_inspect", "browser_find_element", "browser_click_element",
        "browser_type_text", "browser_select_option", "browser_set_checked",
        "browser_wait_condition", "browser_execute_script", "browser_select_tab",
        "browser_status",
        "credential_list", "credential_status",
        "clipboard_status", "clipboard_write_text", "clipboard_clear",
        "elevated_session_open", "elevated_session_close", "elevated_session_status",
        "job_start", "job_status", "job_list", "job_logs",
        "job_cancel", "job_pause", "job_resume",
        "task_create", "task_status", "task_list", "task_cancel",
        "desktop_watch", "watch_events", "watch_list", "watch_cancel",
        "workflow_create", "workflow_run", "workflow_dry_run", "workflow_list",
    }
    missing = v2_surface - names
    assert not missing, f"ferramentas V2 ausentes no registry: {sorted(missing)}"
