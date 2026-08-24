"""Testes prompt11_2 — consistência de estados Proxmox/HA + refresh do monitor.

Cobre:
    * §3/§4     fonte única de enabled; enabled=true + sem token é
      UNCONFIGURED e NUNCA responde PROXMOX_DISABLED;
    * §5        regression test explícito do estado divergente observado na UI;
    * §11       save flow com merge por chave (toggle não apaga URL);
    * §15       self-signed + verificação ON → PROXMOX_TLS_ERROR (nunca
      AUTH_FAILED, nunca downgrade automático de TLS);
    * §16-§19   sucesso autenticado do monitor Homelab atualiza last_success
      → stale=false → READY.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.integrations.base import IntegrationError  # noqa: E402


class StubBroker:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def create(self, credential_id, secret, *, kind="generic", description="",
               approval_id=None, operator_direct=False):
        self.store[credential_id] = secret
        return {"success": True, "credential_id": credential_id}

    def resolve(self, credential_id):
        return self.store.get(credential_id)

    def delete(self, credential_id):
        return self.store.pop(credential_id, None) is not None


@pytest.fixture()
def pm_env(tmp_path, monkeypatch):
    from app.integrations.proxmox import config as mod

    monkeypatch.setattr(mod, "CONFIG_PATH", tmp_path / "proxmox-config.json")
    monkeypatch.setattr(mod, "load_runtime_settings", lambda: {})
    broker = StubBroker()
    monkeypatch.setattr(mod, "_broker", lambda: broker)
    yield mod, broker, tmp_path


@pytest.fixture()
def ha_env(tmp_path, monkeypatch):
    from app.integrations import home_assistant_profiles as mod

    monkeypatch.setattr(mod, "PROFILES_PATH", tmp_path / "ha-profiles.json")
    monkeypatch.setattr(mod, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.setattr(mod, "_last_monitor_record_monotonic", 0.0)
    monkeypatch.delenv("NYRA_HOME_ASSISTANT_TOKEN", raising=False)
    broker = StubBroker()
    monkeypatch.setattr(mod, "_broker", lambda: broker)
    yield mod, broker, tmp_path


class StubProxmoxRuntime:
    configured = False

    def set_credentials(self, base_url, token_id, token_secret, *, verify_ssl=None):
        self.configured = bool(base_url and token_id and token_secret)

    async def version(self):
        return {"version": "8.2.4"}

    async def nodes(self):
        return [{"node": "pve", "status": "online"}]

    async def virtual_machines(self):
        return []

    async def storage(self):
        return []


# ====================================================== Proxmox single source


class TestProxmoxEnabledSingleSource:
    def test_enabled_overlay_no_token_is_unconfigured_never_disabled(self, pm_env):
        """§5 REGRESSÃO: o bug real era Habilitada=Sim + test=PROXMOX_DISABLED.

        Runtime overlay diz enabled=true enquanto as settings legadas (.env)
        dizem false. A resolução ÚNICA precisa enxergar o overlay em TODAS as
        superfícies: status UNCONFIGURED e teste PROXMOX_UNCONFIGURED.
        """
        mod, _, _ = pm_env
        mod.load_runtime_settings = lambda: {"proxmox_enabled": True}  # type: ignore[method-assign]
        settings = Settings(proxmox_enabled=False,
                            proxmox_url="https://192.168.1.2:8006")
        services = type("S", (), {"settings": settings,
                                  "proxmox": StubProxmoxRuntime()})()

        config = mod.load_config(settings)
        assert config["enabled"] is True  # overlay vence .env legado

        status = mod.public_status(services)
        assert status["enabled"] is True
        assert status["configured"] is False
        assert status["state"] == "UNCONFIGURED"
        assert status["health"] == "API Token ausente"

    async def test_test_connection_matches_status_never_disabled(self, pm_env):
        """§4/§5: mesma resolução no Test Connection — nunca PROXMOX_DISABLED."""
        mod, _, _ = pm_env
        mod.load_runtime_settings = lambda: {"proxmox_enabled": True}  # type: ignore[method-assign]
        settings = Settings(proxmox_enabled=False,
                            proxmox_url="https://192.168.1.2:8006")
        services = type("S", (), {"settings": settings,
                                  "proxmox": StubProxmoxRuntime()})()

        result = await mod.test_connection(services)
        assert result["ok"] is False
        assert result["state"] == "UNCONFIGURED"
        assert result["error_code"] == "PROXMOX_UNCONFIGURED"
        assert result["error_code"] != "PROXMOX_DISABLED"

    async def test_disabled_is_disabled_everywhere(self, pm_env):
        """§4: integração realmente desabilitada → DISABLED coerente."""
        mod, _, _ = pm_env
        mod.load_runtime_settings = lambda: {"proxmox_enabled": False}  # type: ignore[method-assign]
        settings = Settings(proxmox_enabled=True)  # legado divergente: ignorado
        services = type("S", (), {"settings": settings,
                                  "proxmox": StubProxmoxRuntime()})()

        status = mod.public_status(services)
        assert status["enabled"] is False
        assert status["state"] == "DISABLED"
        result = await mod.test_connection(services)
        assert result["state"] == "DISABLED"
        assert result["error_code"] == "PROXMOX_DISABLED"

    def test_precedence_file_over_overlay_over_settings(self, pm_env):
        mod, _, _ = pm_env
        mod.save_config({"url": "https://10.0.0.5:8006", "enabled": True})
        mod.load_runtime_settings = lambda: {"proxmox_enabled": False,  # type: ignore[method-assign]
                                             "proxmox_url": "http://overlay"}
        settings = Settings(proxmox_enabled=True, proxmox_url="http://legacy")

        config = mod.load_config(settings)
        assert config["enabled"] is True            # arquivo > overlay
        assert config["url"] == "https://10.0.0.5:8006"

        stored = json.loads(mod.CONFIG_PATH.read_text(encoding="utf-8"))
        del stored["enabled"]                        # sem chave no arquivo
        mod.CONFIG_PATH.write_text(json.dumps(stored), encoding="utf-8")
        config = mod.load_config(settings)
        assert config["enabled"] is False            # overlay > settings legadas

    def test_toggle_enabled_preserves_saved_url(self, pm_env):
        """§11: toggle de enabled não pode apagar a URL salva."""
        mod, _, _ = pm_env
        mod.save_config({"url": "https://192.168.1.2:8006", "enabled": True,
                         "timeout_seconds": 12})
        mod.set_enabled(False)
        config = mod.load_config(Settings())
        assert config["enabled"] is False
        assert config["url"] == "https://192.168.1.2:8006"
        assert config["timeout_seconds"] == 12.0

    async def test_integration_action_enable_persists_to_authoritative_file(
            self, pm_env, monkeypatch):
        """§3/§11: enable/disable via Integration Center grava a fonte única."""
        from app.integrations import center as center_mod

        mod, _, _ = pm_env
        runtime_writes: list[dict] = []
        monkeypatch.setattr("app.core.runtime_settings.save_runtime_settings",
                            lambda updates: runtime_writes.append(updates))
        settings = Settings(proxmox_enabled=False)
        services = type("S", (), {"settings": settings,
                                  "proxmox": StubProxmoxRuntime()})()

        result = await center_mod.integration_action(services, "proxmox", "enable")
        assert result == {"id": "proxmox", "enabled": True,
                          "restart_required": False}
        config = mod.load_config(settings)
        assert config["enabled"] is True             # persistiu no arquivo
        assert settings.proxmox_enabled is True      # espelho em memória
        assert runtime_writes == [{"proxmox_enabled": True}]


# ============================================================== Proxmox TLS


class TestProxmoxTlsError:
    async def test_self_signed_with_verification_maps_to_tls_error(
            self, pm_env, monkeypatch):
        """§15: self-signed + verify ON → PROXMOX_TLS_ERROR (não AUTH_FAILED)."""
        from app.integrations.proxmox.client import ProxmoxReadOnlyClient

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                "self-signed certificate in certificate chain",
                request=request,
            )

        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(
            "app.integrations.proxmox.client.httpx.AsyncClient", factory)
        client = ProxmoxReadOnlyClient("https://192.168.1.2:8006",
                                       "op@pve!tok", "secret", verify_ssl=True)
        with pytest.raises(IntegrationError) as exc:
            await client.version()
        assert exc.value.code == "PROXMOX_TLS_ERROR"

    async def test_tls_error_state_in_test_connection_and_status(self, pm_env):
        mod, broker, _ = pm_env
        mod.save_config({"url": "https://192.168.1.2:8006", "enabled": True})
        broker.store["proxmox_api_token_id"] = "op@pve!tok"
        broker.store["proxmox_api_token_secret"] = "sec"

        class TlsFailClient(StubProxmoxRuntime):
            configured = True

            async def version(self):
                raise IntegrationError("PROXMOX_TLS_ERROR", "certificado recusado")

        settings = Settings(proxmox_enabled=True)
        services = type("S", (), {"settings": settings,
                                  "proxmox": TlsFailClient()})()

        result = await mod.test_connection(services)
        assert result["state"] == "TLS_ERROR"
        assert result["error_code"] == "PROXMOX_TLS_ERROR"  # §15: não AUTH_FAILED

        status = mod.public_status(services)
        assert status["state"] == "TLS_ERROR"


# ============================================================ Rotas (§24)


class TestRoutesHotfixV11_2:
    def test_proxmox_no_token_ui_flow_is_unconfigured(self, tmp_path, monkeypatch):
        """§24 E2E: Integrações → Proxmox → Salvar não-secreto → Testar.

        Com enabled=true (overlay) e SEM token, o teste real retorna
        UNCONFIGURED — nunca PROXMOX_DISABLED.
        """
        from tests.test_ha_proxmox_ui_v11 import build_route_client

        from app.integrations.proxmox import config as pm_mod

        broker = StubBroker()

        monkeypatch.setattr(pm_mod, "CONFIG_PATH", tmp_path / "proxmox-config.json")
        monkeypatch.setattr(pm_mod, "load_runtime_settings",
                            lambda: {"proxmox_enabled": True})
        monkeypatch.setattr(pm_mod, "_broker", lambda: broker)

        settings = Settings(proxmox_enabled=False,
                            proxmox_url="https://192.168.1.2:8006")
        client = build_route_client(type("S", (), {
            "settings": settings,
            "proxmox": StubProxmoxRuntime(),
            "homelab": None,
        })())

        initial = client.get("/api/proxmox/config").json()
        assert initial["enabled"] is True
        assert initial["configured"] is False
        assert initial["state"] == "UNCONFIGURED"
        assert initial["health"] == "API Token ausente"

        saved = client.put("/api/proxmox/config",
                           json={"url": "https://192.168.1.2:8006",
                                 "timeout_seconds": 10}).json()
        assert saved["success"] is True

        result = client.post("/api/proxmox/test").json()
        assert result["error_code"] == "PROXMOX_UNCONFIGURED"
        assert result["state"] == "UNCONFIGURED"

        after = client.get("/api/proxmox/config").json()
        assert after["state"] == "UNCONFIGURED"
        assert after["url"] == "https://192.168.1.2:8006"
        assert after["timeout_seconds"] == 10.0


# ============================================================ HA stale fix


class TestHAMonitorRefresh:
    def _ready_profile(self, mod, *, tested_at: float) -> None:
        mod.upsert_profile({"profile_id": "ha-vm", "name": "HA VM",
                            "url": "http://192.168.1.200", "enabled": True})
        data = mod._load_store()
        data["active_profile"] = "ha-vm"
        data["profiles"][0]["last_test"] = {
            "ok": True, "authenticated": True, "core_version": "2026.8.3",
            "tested_at": tested_at,
        }
        mod._save_store(data)

    async def test_monitor_success_refreshes_stale_to_ready(self, ha_env):
        """§19 REGRESSÃO: sucesso há 954s mostrava STALE indefinido.

        O monitor Homelab sondava HA autenticado com sucesso mas NUNCA
        atualizava last_success da fonte da UI. Agora registra e volta READY.
        """
        mod, _, _ = ha_env
        self._ready_profile(mod, tested_at=time.time() - 954)

        settings = Settings(home_assistant_enabled=True,
                            home_assistant_url="http://192.168.1.200")
        before = await mod.unified_ha_state(type("S", (), {"settings": settings})())
        assert before["state"] == "STALE"  # reproduz o sintoma observado

        recorded = mod.record_monitor_success(
            {"version": "2026.8.3", "state": "RUNNING"},
            base_url="http://192.168.1.200",
        )
        assert recorded is True

        after = await mod.unified_ha_state(type("S", (), {"settings": settings})())
        assert after["state"] == "READY"          # stale=false → READY
        assert after["last_success"] is not None
        assert time.time() - float(after["last_success"]) < 60

    def test_monitor_cooldown_avoids_rewrites(self, ha_env):
        mod, _, _ = ha_env
        self._ready_profile(mod, tested_at=time.time())
        assert mod.record_monitor_success({}, base_url="http://192.168.1.200") is True
        assert mod.record_monitor_success({}, base_url="http://192.168.1.200") is False

    def test_monitor_skips_mismatched_base_url(self, ha_env):
        mod, _, _ = ha_env
        self._ready_profile(mod, tested_at=time.time() - 954)
        assert mod.record_monitor_success({}, base_url="http://10.9.9.9:8123") is False
        vm = mod.get_profile("ha-vm")
        assert vm["last_test"]["tested_at"] <= time.time() - 900  # intocado

    async def test_monitor_without_profile_url_records_nothing(self, ha_env):
        """§18: store padrão (sem URL configurada) não recebe registro."""
        mod, _, _ = ha_env
        assert mod.record_monitor_success({}) is False

    def test_monitor_skips_disabled_profile(self, ha_env):
        mod, _, _ = ha_env
        mod.upsert_profile({"profile_id": "ha-vm", "name": "HA VM",
                            "url": "http://192.168.1.200", "enabled": False})
        data = mod._load_store()
        data["active_profile"] = "ha-vm"
        mod._save_store(data)
        assert mod.record_monitor_success({}, base_url="http://192.168.1.200") is False

    async def test_controller_health_loop_records_success(self, ha_env, monkeypatch,
                                                          tmp_path):
        """§17/§19 ponta a ponta: host_status ONLINE → last_success atualizado."""
        from tests.test_ha_proxmox_ui_v11 import recording_transport

        mod, _, _ = ha_env
        self._ready_profile(mod, tested_at=time.time() - 954)

        from app.events import EventBus
        from app.homelab.controller import HomelabControlPlane
        from app.homelab.history import HomelabHistory
        from app.homelab.registry import HomelabHostRegistry
        from app.integrations.home_assistant import HomeAssistantClient
        from app.tools.shell_approval import ShellApprovalGate
        from tests.test_homelab_control_plane import (
            FakeProxmoxClient,
            FakeRemoteShell,
            sample_hosts,
        )

        recorder: list[dict] = []
        transport = recording_transport(recorder)
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(
            "app.integrations.home_assistant.httpx.AsyncClient", factory)

        settings = Settings(
            homelab_enabled=True,
            homelab_registry_path=tmp_path / "registry.yaml",
            homelab_default_timeout_seconds=2.0,
            homelab_overview_cache_seconds=0,
            database_path=tmp_path / "nyra-test.db",
            home_assistant_enabled=True,
            home_assistant_url="http://192.168.1.200",
            home_assistant_token="monitor-tok-refresh",
        )
        plane = HomelabControlPlane(
            settings, EventBus(), ShellApprovalGate(), FakeRemoteShell(),
            history=HomelabHistory(settings.database_path),
            registry=HomelabHostRegistry(path=None, hosts=sample_hosts()),
            proxmox=FakeProxmoxClient(),
            home_assistant=HomeAssistantClient(settings.home_assistant_url,
                                               settings.home_assistant_token),
        )
        health = await plane.host_status("home_assistant", force=True)
        assert health.integration_state.value == "ONLINE"

        snapshot = await mod.unified_ha_state(
            type("S", (), {"settings": settings})())
        assert snapshot["state"] == "READY"           # §19: stale=false → READY
