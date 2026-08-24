"""Testes da Operations UI V3 (prompt11).

Cobre: Feature Control Center (toggles reais + persistência), Settings
Service V3 (schema/validação/export sem segredos), Integration Center,
Home Assistant Profiles (incl. physical nunca contatado), VoiceProcessorBridge
(loopback-only, breaker, fallback), release/about/version consistency e
sequence id do EventBus.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.capabilities import (
    SPECS,
    get_capabilities,
    set_capability,
    capability_definitions,
)
from app.core.config import Settings
from app.core.release_info import APP_VERSION, about_payload, release_health
from app.core.settings_registry import (
    ENTRIES,
    SENSITIVE_KEYS,
    describe_entries,
    export_config,
    get_settings_v3,
    update_setting,
)
from app.events import EventBus, EventType


# ---------------------------------------------------------------------------
# Stubs leves de services
# ---------------------------------------------------------------------------

class StubSentinel:
    def __init__(self) -> None:
        self._enabled = False

    def status(self) -> dict:
        return {
            "enabled": self._enabled,
            "state": "DISABLED" if not self._enabled else "CONNECTED",
            "host": None,
            "instance_id": None,
            "sentinel_version": None,
            "bridge_version": "1",
            "connected_since": None,
            "last_event": None,
            "events_received": 0,
            "reconnect_count": 0,
            "token_configured": False,
            "last_error": "",
            "uptime_seconds": 1.0,
        }

    def config(self):
        current = self._enabled

        class _Cfg:
            enabled = current

            @staticmethod
            def model_copy(update=None):
                return SimpleNamespace(enabled=bool((update or {}).get("enabled", current)))

        return _Cfg()

    async def update(self, value):
        self._enabled = bool(getattr(value, "enabled", False))
        return self.status()

    async def test_connection(self):
        return {"ok": False, "state": "OFFLINE"}


class StubHost:
    host_id = "openwrt"
    address = "192.168.1.1"
    reachable = True
    overall_state = "HEALTHY"
    integration_error_code = None
    observed_at = 123.0
    probes = [SimpleNamespace(kind="icmp", success=True, latency_ms=2.5)]


class StubOverview:
    generated_at = 1.0
    cached = False
    enabled = True
    hosts = [StubHost]
    summary = {"reachable": 1}


class StubHAClient:
    def __init__(self) -> None:
        self.base_url = ""
        self.token = ""

    auth_missing = False

    def set_credentials(self, base_url: str, token: str) -> None:
        self.base_url = base_url
        self.token = token


class StubHomelab:
    def __init__(self) -> None:
        self.home_assistant = StubHAClient()

    async def overview(self, force: bool = False):
        return StubOverview()

    async def ha_status(self):
        return {"enabled": True, "configured": False, "error_code": "HA_AUTH_MISSING"}


class StubProxmox:
    configured = False


class StubListening:
    def __init__(self) -> None:
        self.enabled = True

    def status(self):
        return {"enabled": self.enabled, "muted": False, "microphone": True}

    async def set_enabled(self, value: bool):
        self.enabled = value
        return {"enabled": value}


class StubNetworkWatch:
    def status(self):
        return {"enabled": False, "running": False}

    async def stop(self):
        return {}


def make_services(settings: Settings) -> SimpleNamespace:
    return SimpleNamespace(
        settings=settings,
        sentinel=StubSentinel(),
        homelab=StubHomelab(),
        proxmox=StubProxmox(),
        listening=StubListening(),
        network_watch=StubNetworkWatch(),
        conversation=SimpleNamespace(
            state=SimpleNamespace(value="IDLE"),
            audio_settings=lambda: {},
        ),
        tts=SimpleNamespace(name="kokoro"),
        stt=SimpleNamespace(name="faster-whisper"),
        llm=SimpleNamespace(name="ollama"),
        event_bus=None,
    )


@pytest.fixture()
def runtime_file(tmp_path, monkeypatch):
    """Redireciona a persistência runtime para tmp_path em todos os consumidores."""
    from app.core import runtime_settings

    path = tmp_path / "settings-v33.json"
    monkeypatch.setattr(
        "app.core.capabilities.save_runtime_settings",
        lambda updates, _path=path: runtime_settings.save_runtime_settings(updates, _path),
    )
    monkeypatch.setattr(
        "app.core.settings_registry.save_runtime_settings",
        lambda updates, _path=path: runtime_settings.save_runtime_settings(updates, _path),
    )
    return path


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

async def test_capability_catalog_covers_mandatory_ids():
    ids = {spec.id for spec in SPECS}
    mandatory = {
        "local_operator", "desktop_control", "ui_automation", "vision",
        "browser_control", "persistent_jobs", "task_planner",
        "workflow_engine", "recovery_engine", "desktop_watcher", "watchdog",
        "proactive_operator", "homelab_control_plane", "sentinel",
        "home_assistant", "proxmox", "openwrt", "voice_engine",
        "external_voice_processor", "desktop_presence",
    }
    assert mandatory <= ids


async def test_capabilities_snapshot_shape(runtime_file):
    settings = Settings(home_assistant_url="http://127.0.0.1:8123")
    services = make_services(settings)
    payload = await get_capabilities(services)
    by_id = {c["id"]: c for c in payload["capabilities"]}
    assert payload["summary"]["total"] == len(SPECS)
    vision = by_id["vision"]
    assert vision["toggleable"] is True
    assert vision["enabled"] is True  # default da Settings
    task_planner = by_id["task_planner"]
    assert task_planner["toggleable"] is False
    ha = by_id["home_assistant"]
    assert ha["runtime_state"] in {"READY", "UNCONFIGURED"}
    sentinel = by_id["sentinel"]
    assert sentinel["runtime_state"] == "DISABLED"


async def test_toggle_non_hot_marks_restart_required_and_persists(runtime_file):
    settings = Settings()
    services = make_services(settings)
    result = await set_capability(services, "vision", False)
    assert result["enabled"] is False
    assert result["restart_required"] is True
    assert settings.vision_enabled is False
    persisted = json.loads(runtime_file.read_text(encoding="utf-8"))
    assert persisted["vision_enabled"] is False


async def test_hot_toggle_listening_applies_immediately(runtime_file):
    settings = Settings()
    services = make_services(settings)
    result = await set_capability(services, "always_listening", False)
    assert result["restart_required"] is False
    assert result["verification"]["applied_immediately"] is True
    assert services.listening.enabled is False
    persisted = json.loads(runtime_file.read_text(encoding="utf-8"))
    assert persisted["always_listening_enabled"] is False


async def test_toggle_unknown_capability_raises(runtime_file):
    with pytest.raises(KeyError):
        await set_capability(make_services(Settings()), "nao_existe", True)


async def test_derived_capability_not_toggleable(runtime_file):
    with pytest.raises(PermissionError):
        await set_capability(make_services(Settings()), "task_planner", False)


def test_capability_definitions_are_documented():
    for definition in capability_definitions():
        assert definition["description"], definition["id"]
        assert definition["consumer"], definition["id"]


# ---------------------------------------------------------------------------
# Settings Service V3
# ---------------------------------------------------------------------------

def test_settings_schema_keys_exist_on_settings_model():
    model_fields = set(Settings.model_fields.keys())
    missing = [e.key for e in ENTRIES if e.key not in model_fields]
    assert missing == []


def test_settings_get_masks_secrets():
    settings = Settings(proxmox_token_secret="x" * 24, openwrt_password="")
    payload = get_settings_v3(settings)
    by_key = {item["key"]: item for item in payload["settings"]}
    secret_entry = by_key["proxmox_token_secret"]
    assert secret_entry["sensitive"] is True
    assert secret_entry["current"] == {"configured": True}
    assert by_key["openwrt_password"]["current"] == {"configured": False}
    raw = json.dumps(payload)
    assert "xxx" * 8 not in raw


def test_setting_update_validates_enum_and_range():
    settings = Settings()
    updated = update_setting(settings, "listening_mode", "push_to_talk")
    assert updated["persisted"] is True
    assert settings.listening_mode == "push_to_talk"
    with pytest.raises(Exception) as enum_error:
        update_setting(settings, "listening_mode", "invalido")
    assert getattr(enum_error.value, "error_code", "") == "SETTINGS_INVALID_OPTION"
    with pytest.raises(Exception) as range_error:
        update_setting(settings, "audio_volume", 7.5)
    assert getattr(range_error.value, "error_code", "") == "SETTINGS_OUT_OF_RANGE"


def test_setting_update_rejects_secrets():
    with pytest.raises(PermissionError):
        update_setting(Settings(), "sentinel_bridge_token", "segredo")


def test_config_export_has_no_secret_values():
    settings = Settings(proxmox_token_secret="supersecreto", home_assistant_url="http://x")
    about = about_payload(make_services(settings))
    exported = export_config(settings, about)
    text = json.dumps(exported)
    assert "supersecreto" not in text
    secret_rows = [row for row in exported["settings"] if row["key"] in SENSITIVE_KEYS]
    assert secret_rows
    for row in secret_rows:
        assert isinstance(row["value"], dict)
        assert set(row["value"].keys()) == {"configured"}


def test_describe_entries_categories_cover_expected():
    categories = {entry["category"] for entry in describe_entries()}
    expected = {"general", "ai", "voice", "desktop", "automation", "homelab",
                "integrations", "privacy", "developer"}
    assert expected <= categories


# ---------------------------------------------------------------------------
# Integration Center
# ---------------------------------------------------------------------------

async def test_integrations_status_cards(tmp_path, runtime_file, monkeypatch):
    from app.integrations import home_assistant_profiles as ha_profiles_mod
    from app.integrations.proxmox import config as pm_config_mod
    from app.integrations.center import integrations_status

    # Hermeticidade: nenhum teste pode ler o ha-profiles.json / proxmox-config
    # reais do operador (estados da máquina real vazariam no resultado).
    monkeypatch.setattr(ha_profiles_mod, "PROFILES_PATH", tmp_path / "ha-profiles.json")
    monkeypatch.setattr(ha_profiles_mod, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.delenv("NYRA_HOME_ASSISTANT_TOKEN", raising=False)
    monkeypatch.setattr(pm_config_mod, "CONFIG_PATH", tmp_path / "proxmox-config.json")

    settings = Settings(home_assistant_url="", homelab_enabled=True,
                        proxmox_enabled=True)
    # Determinístico: o .env real da máquina pode conter token legado válido;
    # este teste cobre o cenário sem nenhuma credencial.
    settings.home_assistant_token = ""
    services = make_services(settings)
    payload = await integrations_status(services)
    cards = payload["integrations"]
    assert set(cards.keys()) == {"sentinel", "home_assistant", "proxmox", "openwrt"}
    assert cards["sentinel"]["state"] == "DISABLED"
    assert cards["home_assistant"]["state"] == "UNCONFIGURED"
    assert cards["home_assistant"]["authentication"] == "AUSENTE"
    assert cards["home_assistant"]["connected"] is False
    assert cards["proxmox"]["state"] == "UNCONFIGURED"
    assert cards["proxmox"]["auth_configured"] is False
    openwrt = cards["openwrt"]
    assert openwrt["connected"] is True
    assert openwrt["latency_ms"] == 2.5


async def test_integrations_ha_ready_requires_auth(tmp_path, runtime_file, monkeypatch):
    """Invariante §13: Auth Ausente NUNCA resulta em READY/connected."""
    from app.integrations import home_assistant_profiles as ha_profiles_mod
    from app.integrations.proxmox import config as pm_config_mod
    from app.integrations.center import integrations_status

    monkeypatch.setattr(ha_profiles_mod, "PROFILES_PATH", tmp_path / "ha-profiles.json")
    monkeypatch.setattr(ha_profiles_mod, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.delenv("NYRA_HOME_ASSISTANT_TOKEN", raising=False)
    monkeypatch.setattr(pm_config_mod, "CONFIG_PATH", tmp_path / "proxmox-config.json")

    settings = Settings(home_assistant_url="http://192.168.1.200",
                        homelab_enabled=True, proxmox_enabled=True)
    settings.home_assistant_token = ""
    services = make_services(settings)
    payload = await integrations_status(services)
    card = payload["integrations"]["home_assistant"]
    assert card["state"] in {"UNCONFIGURED", "STARTING"}
    assert card["state"] != "READY"
    assert card["authentication"] == "AUSENTE"
    assert card["connected"] is False


async def test_integration_action_enable_persists(runtime_file, monkeypatch, tmp_path):
    from app.core import runtime_settings as rs
    from app.integrations import center as center_module

    real_save = rs.save_runtime_settings
    target = tmp_path / "settings-v33.json"
    monkeypatch.setattr(rs, "save_runtime_settings",
                        lambda updates, path=None: real_save(updates, target))
    settings = Settings()
    services = make_services(settings)
    result = await center_module.integration_action(services, "home_assistant", "disable")
    assert result == {"id": "home_assistant", "enabled": False}
    assert settings.home_assistant_enabled is False
    persisted = json.loads(target.read_text(encoding="utf-8"))
    assert persisted["home_assistant_enabled"] is False


async def test_openwrt_ssh_auth_failure_is_not_offline():
    """§91 — ping OK + SSH auth failed ⇒ DEGRADED, nunca OFFLINE."""
    from app.integrations.center import _openwrt_card

    class AuthFailHost(StubHost):
        reachable = True
        overall_state = "DEGRADED"
        integration_error_code = "SSH_AUTH_FAILED"

    class Overview:
        hosts = [AuthFailHost()]
        summary = {"reachable": 1}
        generated_at = 1.0
        cached = False
        enabled = True

    class Homelab:
        async def overview(self, force=False):
            return Overview()

    services = make_services(Settings())
    services.homelab = Homelab()
    card = await _openwrt_card(services)
    assert card["state"] == "DEGRADED"
    assert "SSH_AUTH_FAILED" in str(card["health"])


# ---------------------------------------------------------------------------
# Home Assistant Profiles
# ---------------------------------------------------------------------------

@pytest.fixture()
def ha_store(tmp_path, monkeypatch):
    from app.integrations import home_assistant_profiles as mod

    profiles_path = tmp_path / "ha-profiles.json"
    secrets_dir = tmp_path / "secrets"
    monkeypatch.setattr(mod, "PROFILES_PATH", profiles_path)
    monkeypatch.setattr(mod, "SECRETS_DIR", secrets_dir)
    monkeypatch.delenv("NYRA_HOME_ASSISTANT_TOKEN", raising=False)
    yield mod, secrets_dir


async def test_ha_default_profiles_seed(ha_store):
    mod, _ = ha_store
    data = mod.list_profiles()
    ids = [p["profile_id"] for p in data["profiles"]]
    assert ids == ["ha-vm", "ha-physical"]
    physical = data["profiles"][1]
    assert physical["status"] == "DISABLED"


async def test_ha_physical_profile_never_contacted(ha_store):
    """§191 — profile físico desabilitado não gera conexão."""
    mod, secrets_dir = ha_store
    services = make_services(Settings())
    with pytest.raises(PermissionError):
        await mod.test_profile(services, "ha-physical")
    assert not secrets_dir.exists() or list(secrets_dir.glob("*.txt")) == []


async def test_ha_activate_applies_runtime_credentials(ha_store):
    mod, _ = ha_store
    mod.upsert_profile({
        "profile_id": "ha-vm", "name": "Home Assistant VM",
        "url": "http://192.168.1.200", "enabled": True, "priority": 1,
    })
    services = make_services(Settings())
    mod.set_profile_token(services, "ha-vm", "t" * 40)
    result = await mod.activate_profile(services, "ha-vm")
    assert result["active_profile"] == "ha-vm"
    assert result["auth_configured"] is True
    assert result["runtime_applied"] is True
    assert services.homelab.home_assistant.base_url == "http://192.168.1.200"
    assert services.homelab.home_assistant.token == "t" * 40
    listing = mod.list_profiles()
    vm = next(p for p in listing["profiles"] if p["profile_id"] == "ha-vm")
    assert vm["auth_configured"] is True


async def test_ha_upsert_validates_url(ha_store):
    mod, _ = ha_store
    with pytest.raises(ValueError):
        mod.upsert_profile({"profile_id": "bad", "name": "Bad", "url": "ftp://x"})


# ---------------------------------------------------------------------------
# VoiceProcessorBridge
# ---------------------------------------------------------------------------

def _patch_http(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    original_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return original_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return transport


@pytest.fixture()
def bridge_store(tmp_path, monkeypatch):
    from app.core import runtime_settings
    from app.speech import external_bridge as mod

    path = tmp_path / "settings-v33.json"
    monkeypatch.setattr(mod, "save_runtime_settings",
                        lambda updates: runtime_settings.save_runtime_settings(updates, path))
    return mod, path


async def test_bridge_rejects_non_loopback_endpoint(bridge_store):
    mod, _ = bridge_store
    bridge = mod.VoiceProcessorBridge(Settings())
    with pytest.raises(ValueError):
        await bridge.update({"endpoint": "http://192.168.1.55:9000"})
    with pytest.raises(ValueError):
        await bridge.update({"endpoint": "http://nyra.example.com:9000"})


async def test_bridge_probe_healthy_negotiates_capabilities(bridge_store, monkeypatch):
    mod, path = bridge_store

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={
            "name": "test-processor", "version": "0.1", "healthy": True,
            "capabilities": {"stt": True, "tts": True, "vad": True,
                             "aec": False, "ns": True, "streaming": True},
        })

    _patch_http(monkeypatch, handler)
    bridge = mod.VoiceProcessorBridge(Settings())
    await bridge.update({"enabled": True})
    status = bridge.cached_status()
    assert status["health"] == "HEALTHY"
    assert status["capabilities"]["stt"] is True
    assert status["capabilities"]["aec"] is False
    assert status["latency_ms"] is not None


async def test_bridge_breaker_opens_after_repeated_failures(bridge_store, monkeypatch):
    mod, _ = bridge_store

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _patch_http(monkeypatch, handler)
    bridge = mod.VoiceProcessorBridge(Settings())
    await bridge.update({"enabled": True})
    for _ in range(mod.BREAKER_FAILURE_THRESHOLD):
        result = await bridge.test()
        assert result["ok"] is False
    assert bridge.cached_status()["breaker_open"] is True
    blocked = await bridge.probe()
    assert blocked.get("error_code") == "BRIDGE_BREAKER_OPEN"


async def test_bridge_fallback_flag_when_down(bridge_store, monkeypatch):
    """§128/§149 — processor caído ⇒ fallback interno sinalizado honestamente."""
    mod, _ = bridge_store

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    _patch_http(monkeypatch, handler)
    bridge = mod.VoiceProcessorBridge(Settings())
    await bridge.update({"enabled": True})
    await bridge.test()
    status = bridge.cached_status()
    assert status["fallback_internal_active"] is True
    assert status["health"] == "OFFLINE"


# ---------------------------------------------------------------------------
# Release / About / Version consistency (§206-§209)
# ---------------------------------------------------------------------------

def test_about_reports_unified_version():
    services = make_services(Settings(llm_model="qwen2.5:3b"))
    about = about_payload(services)
    assert about["version"] == APP_VERSION
    assert about["model"] == "qwen2.5:3b"


def test_version_consistency_across_manifests():
    root = Path(__file__).resolve().parents[2]
    backend_toml = (root / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    frontend_pkg = json.loads((root / "frontend" / "package.json").read_text("utf-8"))
    desktop_pkg_path = root / "desktop" / "package.json"
    tauri_conf = json.loads((root / "desktop" / "src-tauri" / "tauri.conf.json").read_text("utf-8"))
    assert f'version = "{APP_VERSION}"' in backend_toml
    assert frontend_pkg["version"] == APP_VERSION
    assert tauri_conf["version"] == APP_VERSION
    if desktop_pkg_path.is_file():
        assert json.loads(desktop_pkg_path.read_text("utf-8"))["version"] == APP_VERSION


def test_release_health_states_without_gate(tmp_path, monkeypatch):
    from app.core import release_info as mod

    monkeypatch.setattr(mod, "DAILY_CHECK_HISTORY",
                        tmp_path / "daily-check-history.jsonl")
    monkeypatch.setattr(mod, "RELEASE_GATE_REPORT",
                        tmp_path / ".tmp" / "release-health.json")
    report = release_health(make_services(Settings()))
    assert report["state"] in {"GREEN", "YELLOW", "RED"}
    ids = {c["id"] for c in report["criteria"]}
    assert {"daily_use_suite", "release_gate", "encoding_audit"} <= ids
    encoding = next(c for c in report["criteria"] if c["id"] == "encoding_audit")
    assert encoding["state"] == "PASS"


def test_release_health_red_on_current_daily_failures(tmp_path, monkeypatch):
    """Closure §20.4: RED somente para falha de validação ATUAL."""
    from app.core import release_info as mod

    history = tmp_path / "history.jsonl"
    document = {
        "generated_at": time.time(),
        "overall": "FAIL",
        "timestamp": "2026-08-23T00:00:00Z",
        "categories": {"llm": {"result": "FAIL"}, "voice": {"result": "PASS"}},
    }
    history.write_text(json.dumps(document) + "\n", encoding="utf-8")
    monkeypatch.setattr(mod, "DAILY_CHECK_HISTORY", history)
    monkeypatch.setattr(mod, "RELEASE_GATE_REPORT", tmp_path / "gate.json")
    report = release_health(make_services(Settings()))
    assert report["state"] == "RED"


def test_release_health_stale_artifact_is_never_red(tmp_path, monkeypatch):
    """Closure §20.2/§20.3: artefato antigo ou sem timestamp → STALE, nunca RED."""
    from app.core import release_info as mod

    stale_history = tmp_path / "stale-history.jsonl"
    stale_document = {
        "generated_at": time.time() - 48 * 3600,
        "overall": "FAIL",
        "timestamp": "2026-08-21T00:00:00Z",
        "categories": {"llm": {"result": "FAIL"}},
    }
    stale_history.write_text(json.dumps(stale_document) + "\n", encoding="utf-8")
    monkeypatch.setattr(mod, "DAILY_CHECK_HISTORY", stale_history)
    monkeypatch.setattr(mod, "RELEASE_GATE_REPORT", tmp_path / "gate.json")
    report = release_health(make_services(Settings()))
    assert report["freshness"] == "STALE"
    assert report["state"] != "RED"

    no_stamp = tmp_path / "no-stamp.jsonl"
    no_stamp.write_text(json.dumps({"overall": "FAIL"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(mod, "DAILY_CHECK_HISTORY", no_stamp)
    report = release_health(make_services(Settings()))
    assert report["state"] != "RED"


# ---------------------------------------------------------------------------
# EventBus sequence id (§158)
# ---------------------------------------------------------------------------

async def test_event_sequence_is_monotonic():
    bus = EventBus(history_size=10)
    first = await bus.publish(EventType.ERROR, operation="a")
    second = await bus.publish(EventType.ERROR, operation="b")
    assert second.seq == first.seq + 1
    assert first.seq > 0
