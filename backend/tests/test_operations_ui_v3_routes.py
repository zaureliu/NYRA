"""Smoke HTTP dos endpoints V3 sobre o router real (prompt11 §245).

Usa um app FastAPI mínimo com services stub para validar contratos,
códigos de status e envelope de erro sem subir LLM/TTS.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_operations_ui_v3 import make_services  # noqa: E402


def build_client(tmp_path: Path) -> TestClient:
    from app.api.routes import router
    from app.core.config import Settings

    app = FastAPI()
    app.include_router(router)
    services = make_services(Settings(home_assistant_url="http://127.0.0.1:8123"))
    app.state.services = services
    return TestClient(app)


def test_capabilities_endpoint_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("KAZUMI_OLLAMA_PRELOAD", "false")
    client = build_client(tmp_path)
    response = client.get("/api/capabilities")
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["total"] >= 20
    vision = next(c for c in payload["capabilities"] if c["id"] == "vision")
    for field in ("enabled", "runtime_state", "health", "restart_required",
                  "toggleable", "configured"):
        assert field in vision


def test_settings_v3_and_secret_rejection(tmp_path):
    client = build_client(tmp_path)
    listing = client.get("/api/settings/v3")
    assert listing.status_code == 200
    assert listing.json()["version"] == 3

    ok = client.put("/api/settings/v3", json={"key": "network_voice_alerts", "value": False})
    assert ok.status_code == 200
    assert ok.json()["persisted"] is True

    secret = client.put("/api/settings/v3", json={"key": "sentinel_bridge_token", "value": "x"})
    assert secret.status_code == 409
    body = secret.json()["detail"]
    assert body["error_code"] == "SETTING_IS_SECRET"

    invalid = client.put("/api/settings/v3", json={"key": "audio_volume", "value": 42})
    assert invalid.status_code == 422


def test_unknown_capability_returns_envelope(tmp_path):
    client = build_client(tmp_path)
    response = client.put("/api/capabilities/inexistente", json={"enabled": False})
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error_code"] == "CAPABILITY_UNKNOWN"
    assert {"error_code", "message", "stage", "recoverable"} <= set(detail.keys())


def test_integrations_status_shape(tmp_path):
    client = build_client(tmp_path)
    response = client.get("/api/integrations/status")
    assert response.status_code == 200
    integrations = response.json()["integrations"]
    assert set(integrations) == {"sentinel", "home_assistant", "proxmox", "openwrt"}


def test_ha_profile_test_disabled_is_409_not_connection(tmp_path, monkeypatch):
    from app.integrations import home_assistant_profiles as mod

    monkeypatch.setattr(mod, "PROFILES_PATH", tmp_path / "profiles.json")
    monkeypatch.setattr(mod, "SECRETS_DIR", tmp_path / "secrets")
    client = build_client(tmp_path)
    # ha-physical nasce desabilitado e SEM url: nunca deve haver tentativa de rede
    response = client.post("/api/home-assistant/profiles/ha-physical/test")
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "HA_PROFILE_DISABLED"


def test_about_release_worldstate_endpoints(tmp_path):
    client = build_client(tmp_path)
    about = client.get("/api/about").json()
    assert about["version"].count(".") >= 1
    health = client.get("/api/release/health").json()
    assert health["state"] in {"GREEN", "YELLOW", "RED"}
    world = client.get("/api/world-state").json()
    assert world["total_observations"] >= 5


def test_voice_profiles_activate_persists(tmp_path):
    client = build_client(tmp_path)
    profiles = client.get("/api/voice/profiles").json()
    assert {p["profile_id"] for p in profiles["profiles"]} == {
        "realtime", "natural", "low_latency", "external_processor",
    }
    activation = client.post("/api/voice/profiles/natural/activate")
    assert activation.status_code == 200
    after = client.get("/api/voice/profiles").json()
    natural = next(p for p in after["profiles"] if p["profile_id"] == "natural")
    assert natural["active"] is True
