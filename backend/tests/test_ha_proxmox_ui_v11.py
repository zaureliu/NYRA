"""Testes prompt11_1 — regressão Home Assistant + configuração Proxmox via UI.

Cobre:
    * §8/§9/§69  probes autenticados; nenhum request sem Bearer para endpoints
      autenticados quando deveria haver token;
    * §13/§61    invariante Auth Ausente → NUNCA READY;
    * §16        critério de estados (configurado/ausente/recusado);
    * §60        resolução de credencial, persistência, profile ativo,
      Authorization do monitor Homelab, consistência de estado, inventário;
    * §51/§50    zero leak do secret em payloads públicos;
    * §59        restauração após restart.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.integrations.base import IntegrationError  # noqa: E402


# --------------------------------------------------------------------- stubs


class StubBroker:
    """Broker hermético: nenhuma escrita no Credential Manager real."""

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

    def list_credentials(self):
        return {"credentials": sorted(self.store)}


@pytest.fixture()
def ha_env(tmp_path, monkeypatch):
    from app.integrations import home_assistant_profiles as mod

    monkeypatch.setattr(mod, "PROFILES_PATH", tmp_path / "ha-profiles.json")
    monkeypatch.setattr(mod, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.delenv("NYRA_HOME_ASSISTANT_TOKEN", raising=False)
    broker = StubBroker()
    monkeypatch.setattr(mod, "_broker", lambda: broker)
    yield mod, broker, tmp_path


@pytest.fixture()
def pm_env(tmp_path, monkeypatch):
    from app.integrations.proxmox import config as mod

    monkeypatch.setattr(mod, "CONFIG_PATH", tmp_path / "proxmox-config.json")
    # prompt11_2: o overlay runtime (settings-v33.json real) é fonte da
    # resolução de enabled — precisa ficar isolado por teste.
    monkeypatch.setattr(mod, "load_runtime_settings", lambda: {})
    broker = StubBroker()
    monkeypatch.setattr(mod, "_broker", lambda: broker)
    yield mod, broker, tmp_path


def recording_transport(recorder, *, status=200, config_payload=None,
                        states_payload=None, root_text="API running."):
    """MockTransport que grava headers/path de cada request feito."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append({
            "path": request.url.path,
            "ua": request.headers.get("user-agent", ""),
            "auth": request.headers.get("authorization", ""),
        })
        if request.url.path == "/api/" or request.url.path == "/api":
            return httpx.Response(status, text=root_text)
        if request.url.path == "/api/config":
            return httpx.Response(status, json=config_payload or {
                "version": "2026.8.3", "state": "RUNNING",
                "location_name": "Casa",
            })
        if request.url.path == "/api/states":
            return httpx.Response(status, json=states_payload or [])
        return httpx.Response(status, json={})

    return httpx.MockTransport(handler)


# ================================================================ HA client


class TestHomeAssistantClientAuth:
    async def test_request_carries_bearer_and_ua(self, monkeypatch):
        from app.integrations.home_assistant import HomeAssistantClient

        recorder: list[dict] = []
        transport = recording_transport(recorder)
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr("app.integrations.home_assistant.httpx.AsyncClient", factory)
        client = HomeAssistantClient("http://192.168.1.200", "tok-1234567890")
        payload = await client.config()

        assert payload["state"] == "RUNNING"
        assert recorder[0]["path"] == "/api/config"
        assert recorder[0]["auth"] == "Bearer tok-1234567890"
        assert recorder[0]["ua"] == "NYRA-Homelab/1.0"

    async def test_no_token_blocks_authenticated_endpoint(self, monkeypatch):
        """§9: sem token, /api/ não é contatado — erro local AUTH_MISSING."""
        from app.integrations.base import IntegrationError
        from app.integrations.home_assistant import HomeAssistantClient

        recorder: list[dict] = []
        transport = recording_transport(recorder)
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr("app.integrations.home_assistant.httpx.AsyncClient", factory)
        client = HomeAssistantClient("http://192.168.1.200", "")
        with pytest.raises(IntegrationError) as exc:
            await client.api_root()
        assert exc.value.code == "HA_AUTH_MISSING"
        assert recorder == []  # nenhum request saiu

    async def test_nonauthenticated_path_allowed_without_token(self, monkeypatch):
        """Sem token, caminho público (ex.: site root) continua permitido."""
        from app.integrations.home_assistant import HomeAssistantClient

        recorder: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorder.append({"path": request.url.path})
            return httpx.Response(200, text="ok")

        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr("app.integrations.home_assistant.httpx.AsyncClient", factory)
        client = HomeAssistantClient("http://192.168.1.200", "")
        response = await client._request("GET", "/")
        assert response.status_code == 200
        assert recorder[0]["path"] == "/"


# ============================================================== HA profiles


class TestHAProfilesRegression:
    def test_status_never_ready_without_auth(self, ha_env):
        """§61 REGRESSÃO: reachable + credencial ausente → status != READY."""
        mod, _, _ = ha_env
        mod.upsert_profile({"profile_id": "ha-vm", "name": "HA VM",
                            "url": "http://192.168.1.200", "enabled": True})
        # Simula registro antigo corrompido com status READY sem token:
        data = mod._load_store()
        data["profiles"][0]["status"] = "READY"
        mod._save_store(data)

        listing = mod.list_profiles()
        vm = next(p for p in listing["profiles"] if p["profile_id"] == "ha-vm")
        assert vm["status"] == "UNCONFIGURED"
        assert vm["auth_configured"] is False

    async def test_probe_without_token_makes_zero_requests(self, ha_env, monkeypatch):
        mod, _, _ = ha_env
        recorder: list[dict] = []
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = recording_transport(recorder)
            return original(*args, **kwargs)

        monkeypatch.setattr(
            "app.integrations.home_assistant_profiles.httpx.AsyncClient", factory)
        result = await mod._probe("http://192.168.1.200", "")
        assert result["error_code"] == "HA_UNCONFIGURED"
        assert recorder == []  # §9: nada saiu sem Bearer

    async def test_probe_sends_bearer_and_ua(self, ha_env, monkeypatch):
        mod, _, _ = ha_env
        recorder: list[dict] = []
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = recording_transport(recorder)
            return original(*args, **kwargs)

        monkeypatch.setattr(
            "app.integrations.home_assistant_profiles.httpx.AsyncClient", factory)
        result = await mod._probe("http://192.168.1.200", "tok-abcdef-123456")

        assert result["ok"] is True
        assert result["authenticated"] is True
        assert len(recorder) == 3  # /api/, /api/config, /api/states
        for item in recorder:
            assert item["auth"] == "Bearer tok-abcdef-123456"
            assert item["ua"] == "NYRA-Homelab/1.0"  # nunca python-httpx default

    async def test_test_profile_auth_failed_on_401(self, ha_env, monkeypatch):
        mod, broker, _ = ha_env
        mod.upsert_profile({"profile_id": "ha-vm", "name": "HA VM",
                            "url": "http://192.168.1.200", "enabled": True})
        broker.store["homeassistant_token_ha-vm"] = "tok-recusado-123456"

        original = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Invalid authentication"})

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(
            "app.integrations.home_assistant_profiles.httpx.AsyncClient", factory)

        services = type("S", (), {"settings": Settings()})()
        result = await mod.test_profile(services, "ha-vm")
        assert result["ok"] is False
        assert result["error_code"] == "HA_AUTH_FAILED"

        vm = mod.get_profile("ha-vm")
        assert vm["status"] == "AUTH_FAILED"  # §10: nunca OFFLINE/READY

    async     def test_legacy_file_token_migrates_to_broker(self, ha_env):
        """§7: credencial legada funcional é reutilizada e migrada em silêncio."""
        mod, broker, tmp_path = ha_env
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir(parents=True)
        legacy_token = "legacy-token-abc-1234567890"
        (secrets_dir / "home-assistant-token-ha-vm.txt").write_text(
            legacy_token + "\n", encoding="utf-8")

        resolved = mod.resolve_profile_token("ha-vm")
        assert resolved == legacy_token          # funcional imediatamente
        assert broker.store.get("homeassistant_token_ha-vm") == legacy_token  # migrado

    def test_settings_only_token_resolves(self, ha_env):
        """§7: token legado via .env/pydantic (só em settings) resolve.

        Regressão real: pydantic carrega NYRA_HOME_ASSISTANT_TOKEN para
        settings.home_assistant_token sem exportar para os.environ — a
        resolução precisa consultar settings como último recurso.
        """
        mod, _, _ = ha_env
        settings = Settings(home_assistant_url="http://192.168.1.200",
                            home_assistant_token="env-loaded-token-xyz")
        assert mod.resolve_profile_token("ha-vm", settings) == "env-loaded-token-xyz"

    def test_startup_apply_never_wipes_functional_client_token(self, ha_env):
        """§7: restaurar perfil após restart não pode apagar token funcional."""
        mod, _, _ = ha_env
        mod.upsert_profile({"profile_id": "ha-vm", "name": "HA VM",
                            "url": "http://192.168.1.200", "enabled": True})
        data = mod._load_store()
        data["active_profile"] = "ha-vm"
        mod._save_store(data)

        class TokenClient:
            def __init__(self) -> None:
                self.base_url = "http://192.168.1.200"
                self.token = "legacy-functional-token"

            @property
            def bearer_token(self) -> str:
                return self.token

            def set_credentials(self, url: str, token: str) -> None:
                self.base_url = url
                self.token = token

        client = TokenClient()
        # Nenhuma fonte de token disponível ao backend (nem settings).
        settings = Settings(home_assistant_enabled=True,
                            home_assistant_url="http://192.168.1.200",
                            home_assistant_token="")
        services = type("S", (), {"settings": settings,
                                  "homelab": type("H", (), {"home_assistant": client})()})()
        summary = mod.apply_active_profile_to_runtime(services)

        assert summary["applied"] is True
        assert client.token == "legacy-functional-token"  # preservado

    def test_startup_apply_restores_settings_legacy_token(self, ha_env):
        """§59: após restart, perfil ativo + credencial resolvível."""
        mod, _, _ = ha_env
        mod.upsert_profile({"profile_id": "ha-vm", "name": "HA VM",
                            "url": "http://192.168.1.200", "enabled": True})
        data = mod._load_store()
        data["active_profile"] = "ha-vm"
        mod._save_store(data)

        class Client:
            def __init__(self) -> None:
                self.base_url = ""
                self.token = ""

            @property
            def bearer_token(self) -> str:
                return self.token

            def set_credentials(self, url: str, token: str) -> None:
                self.base_url = url
                self.token = token

        client = Client()
        settings = Settings(home_assistant_enabled=True,
                            home_assistant_url="",
                            home_assistant_token="env-token-restored")
        services = type("S", (), {"settings": settings,
                                  "homelab": type("H", (), {"home_assistant": client})()})()
        summary = mod.apply_active_profile_to_runtime(services)

        assert summary["applied"] is True
        assert summary["auth_configured"] is True
        assert client.token == "env-token-restored"
        assert client.base_url == "http://192.168.1.200"
        assert settings.home_assistant_url == "http://192.168.1.200"

    def test_set_token_roundtrip_and_public_masking(self, ha_env):
        """§19/§50: após salvar, só metadado auth_configured é exposto."""
        mod, broker, _ = ha_env
        secret = "NYRA_SECRET_LEAK_TEST_a7f3d9c2b1"
        mod.upsert_profile({"profile_id": "ha-vm", "name": "HA VM",
                            "url": "http://192.168.1.200", "enabled": True})
        services = type("S", (), {
            "settings": Settings(home_assistant_url="http://192.168.1.200"),
            "homelab": type("H", (), {"home_assistant": None})(),
        })()
        result = mod.set_profile_token(services, "ha-vm", secret)
        assert result == {"profile_id": "ha-vm", "auth_configured": True}

        serialized = json.dumps(mod.list_profiles(), default=str)
        assert secret not in serialized  # 0 leaks no payload público
        vm = mod.get_profile("ha-vm")
        assert vm["auth_configured"] is True and secret not in json.dumps(vm)

    async def test_unified_state_ready_requires_validated_auth(self, ha_env):
        """§12/§16: READY exige último teste autenticado bem-sucedido."""
        mod, _, _ = ha_env
        import time as _time

        mod.upsert_profile({"profile_id": "ha-vm", "name": "HA VM",
                            "url": "http://192.168.1.200", "enabled": True})
        data = mod._load_store()
        data["active_profile"] = "ha-vm"
        data["profiles"][0]["last_test"] = {
            "ok": True, "authenticated": True, "core_version": "2026.8.3",
            "state": "RUNNING", "entity_count": 42, "latency_ms": 12.5,
            "tested_at": _time.time(),
        }
        mod._save_store(data)

        settings = Settings(home_assistant_enabled=True,
                            home_assistant_url="http://192.168.1.200")
        snapshot = await mod.unified_ha_state(type("S", (), {"settings": settings})())
        assert snapshot["state"] == "READY"
        assert snapshot["authenticated"] is True
        assert snapshot["entity_count"] == 42

    async def test_unified_state_auth_failed_stays_auth_failed(self, ha_env):
        mod, broker, _ = ha_env
        import time as _time

        # Token configurado mas recusado → AUTH_FAILED (§10), nunca OFFLINE.
        broker.store["homeassistant_token_ha-vm"] = "tok-configurado-mas-recusado"
        mod.upsert_profile({"profile_id": "ha-vm", "name": "HA VM",
                            "url": "http://192.168.1.200", "enabled": True})
        data = mod._load_store()
        data["active_profile"] = "ha-vm"
        data["profiles"][0]["last_test"] = {
            "ok": False, "error_code": "HA_AUTH_FAILED",
            "tested_at": _time.time(),
        }
        mod._save_store(data)
        settings = Settings(home_assistant_enabled=True,
                            home_assistant_url="http://192.168.1.200")
        snapshot = await mod.unified_ha_state(type("S", (), {"settings": settings})())
        assert snapshot["state"] == "AUTH_FAILED"
        assert snapshot["state"] != "OFFLINE"


# ====================================================== Monitor authorization


class TestHomelabMonitorAuthorization:
    def build_plane(self, monkeypatch, token: str, tmp_path):
        """Control plane real com client HA real sobre transporte gravador."""
        from tests.test_homelab_control_plane import (
            FakeProxmoxClient,
            FakeRemoteShell,
            sample_hosts,
        )
        from app.events import EventBus
        from app.homelab.controller import HomelabControlPlane
        from app.homelab.history import HomelabHistory
        from app.homelab.registry import HomelabHostRegistry
        from app.integrations.home_assistant import HomeAssistantClient
        from app.tools.shell_approval import ShellApprovalGate

        settings = Settings(
            homelab_enabled=True,
            homelab_registry_path=tmp_path / "registry.yaml",
            homelab_default_timeout_seconds=2.0,
            homelab_overview_cache_seconds=0,
            database_path=tmp_path / "nyra-test.db",
            home_assistant_enabled=True,
            home_assistant_url="http://192.168.1.200",
            home_assistant_token=token,
        )
        recorder: list[dict] = []
        transport = recording_transport(
            recorder,
            config_payload={"version": "2026.8.3", "state": "RUNNING"},
            states_payload=[{"entity_id": "light.x", "state": "on",
                             "attributes": {}, "last_changed": "", "last_updated": ""}],
        )
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(
            "app.integrations.home_assistant.httpx.AsyncClient", factory)

        plane = HomelabControlPlane(
            settings, EventBus(), ShellApprovalGate(), FakeRemoteShell(),
            history=HomelabHistory(settings.database_path),
            registry=HomelabHostRegistry(path=None, hosts=sample_hosts()),
            proxmox=FakeProxmoxClient(),
            home_assistant=HomeAssistantClient(settings.home_assistant_url, token),
        )
        return plane, recorder

    async def test_monitor_requests_carry_bearer(self, monkeypatch, tmp_path):
        """§60: monitor Homelab envia Authorization em todos os requests."""
        plane, recorder = self.build_plane(monkeypatch, "monitor-tok-9876", tmp_path)
        health = await plane.host_status("home_assistant", force=True)

        assert recorder, "monitor deveria ter feito requests"
        assert all(item["auth"] == "Bearer monitor-tok-9876" for item in recorder)
        assert all(item["ua"] == "NYRA-Homelab/1.0" for item in recorder)
        assert health.integration_state.value in {"ONLINE"}

    async def test_monitor_without_token_makes_zero_ha_requests(self, monkeypatch, tmp_path):
        """§9/§15: sem token o monitor não gera invalid-auth no HA."""
        plane, recorder = self.build_plane(monkeypatch, "", tmp_path)
        health = await plane.host_status("home_assistant", force=True)

        ha_calls = [item for item in recorder if item["path"].startswith("/api")]
        assert ha_calls == [], "nenhum request autenticado pode sair sem Bearer"
        assert health.integration_error_code == "HA_AUTH_MISSING"
        assert health.integration_state.value == "INTEGRATION_UNAVAILABLE"

    def test_configuration_status_never_ready_without_token(self, monkeypatch, tmp_path):
        """§61: configuration_status não pode reportar READY sem token."""
        plane, _ = self.build_plane(monkeypatch, "", tmp_path)
        status = plane.configuration_status()
        assert status["home_assistant"] == "UNCONFIGURED"


# ============================================================== Coerência


class TestStateConsistency:
    async def test_homelab_and_integrations_compatible_when_ready(self, ha_env, monkeypatch):
        """§62 REGRESSÃO: autenticado → Homelab e Integrations coerentes."""
        mod, _, tmp_path = ha_env
        import time as _time

        from app.integrations.center import integrations_status

        mod.upsert_profile({"profile_id": "ha-vm", "name": "HA VM",
                            "url": "http://192.168.1.200", "enabled": True})
        data = mod._load_store()
        data["active_profile"] = "ha-vm"
        data["profiles"][0]["last_test"] = {
            "ok": True, "authenticated": True, "core_version": "2026.8.3",
            "state": "RUNNING", "entity_count": 7, "latency_ms": 9.9,
            "tested_at": _time.time(),
        }
        mod._save_store(data)

        settings = Settings(home_assistant_enabled=True,
                            home_assistant_url="http://192.168.1.200")
        services = type("S", (), {"settings": settings})()
        card = (await integrations_status(services))["integrations"]["home_assistant"]
        unified = await mod.unified_ha_state(services)

        assert unified["state"] == "READY"
        assert card["state"] == "READY"
        assert card["connected"] is True
        assert card["authentication"] == "CONFIGURADA"
        # Mesma fonte: valores idênticos entre as duas superfícies.
        assert card["latency_ms"] == unified["latency_ms"]
        assert card["entity_count"] == unified["entity_count"]

    async def test_center_card_not_ready_when_auth_missing(self, ha_env):
        from app.integrations.center import integrations_status

        mod, _, _ = ha_env
        mod.upsert_profile({"profile_id": "ha-vm", "name": "HA VM",
                            "url": "http://192.168.1.200", "enabled": True})
        settings = Settings(home_assistant_enabled=True,
                            home_assistant_url="http://192.168.1.200")
        settings.home_assistant_token = ""
        card = (await integrations_status(type("S", (), {"settings": settings})())
                )["integrations"]["home_assistant"]
        assert card["state"] != "READY"
        assert card["authentication"] == "AUSENTE"


# ============================================================ Proxmox config


class StubProxmoxRuntime:
    def __init__(self) -> None:
        self.creds: tuple[str, str, bool | None] | None = None
        self.configured = False
        self.fail_with: IntegrationError | None = None
        self.version_data = {"version": "8.2.4"}
        self.nodes_data = [{"node": "pve", "status": "online"}]
        self.guests = [
            {"vmid": 103, "name": "web", "type": "qemu", "node": "pve", "status": "stopped"},
            {"vmid": 120, "name": "homeassistant", "type": "qemu", "node": "pve", "status": "running"},
            {"vmid": 201, "name": "pihole", "type": "lxc", "node": "pve", "status": "running"},
        ]
        self.storage_data = [
            {"storage": "local", "plugintype": "dir", "node": "pve",
             "maxdisk": 100.0, "disk": 25.0, "active": True},
        ]

    def set_credentials(self, base_url, token_id, token_secret, *, verify_ssl=None):
        self.creds = (base_url, token_id, token_secret, verify_ssl)
        self.configured = bool(base_url and token_id and token_secret)

    async def version(self):
        if self.fail_with:
            raise self.fail_with
        return dict(self.version_data)

    async def nodes(self):
        if self.fail_with:
            raise self.fail_with
        return [dict(n) for n in self.nodes_data]

    async def virtual_machines(self):
        if self.fail_with:
            raise self.fail_with
        return [dict(g) for g in self.guests]

    async def storage(self):
        if self.fail_with:
            raise self.fail_with
        return [dict(s) for s in self.storage_data]


class StubController:
    def __init__(self, client: StubProxmoxRuntime) -> None:
        self.proxmox = client

    async def proxmox_list_vms(self, include_lxc: bool = True):
        guests = []
        for item in self.proxmox.guests:
            guest_type = "lxc" if item.get("type") == "lxc" else "qemu"
            if guest_type == "lxc" and not include_lxc:
                continue
            guests.append({
                "vmid": item.get("vmid"), "name": item.get("name"),
                "guest_type": guest_type, "node": item.get("node"),
                "status": item.get("status"), "cpu_percent": 1.0,
                "memory_used_bytes": 1, "memory_total_bytes": 2, "uptime_s": 10,
            })
        return guests

    async def proxmox_storage_status(self):
        return [{"storage": s["storage"], "type": s["plugintype"], "node": s["node"],
                 "total_bytes": s["maxdisk"], "used_bytes": s["disk"],
                 "usage_percent": 25.0} for s in self.proxmox.storage_data]


def make_pm_services(settings: Settings, client: StubProxmoxRuntime):
    return type("S", (), {
        "settings": settings,
        "proxmox": client,
        "homelab": StubController(client),
    })()


class TestProxmoxConfig:
    def test_save_rejects_bad_url(self, pm_env):
        mod, _, _ = pm_env
        with pytest.raises(ValueError):
            mod.save_config({"url": "ftp://pve:8006"})

    def test_status_unconfigured_without_token(self, pm_env):
        """§54/§68: antes da credencial o estado é UNCONFIGURED."""
        mod, _, _ = pm_env
        settings = Settings(proxmox_enabled=True, proxmox_url="https://192.168.1.2:8006")
        settings.proxmox_token_id = ""
        settings.proxmox_token_secret = ""
        status = mod.public_status(make_pm_services(settings, StubProxmoxRuntime()))
        assert status["state"] == "UNCONFIGURED"
        assert status["auth_configured"] is False
        assert status["configured"] is False

    def test_disabled_state(self, pm_env):
        mod, _, _ = pm_env
        settings = Settings(proxmox_enabled=False)
        status = mod.public_status(make_pm_services(settings, StubProxmoxRuntime()))
        assert status["state"] == "DISABLED"

    def test_ready_requires_token_and_successful_test(self, pm_env):
        """§69: Proxmox NUNCA fica READY sem API token validado."""
        mod, broker, _ = pm_env
        mod.save_config({"url": "https://192.168.1.2:8006", "enabled": True})
        settings = Settings(proxmox_enabled=True)
        client = StubProxmoxRuntime()
        services = make_pm_services(settings, client)

        # 1) Sem credencial: UNCONFIGURED (§34), mesmo com URL salva.
        status = mod.public_status(services)
        assert status["state"] == "UNCONFIGURED"

        # 2) Credencial configurada, teste ainda não feito: DEGRADED.
        broker.store["proxmox_api_token_id"] = "nyra@pve!ui"
        broker.store["proxmox_api_token_secret"] = "NYRA_SECRET_LEAK_TEST_pmx"
        status = mod.public_status(services)
        assert status["state"] == "DEGRADED"

    async def test_full_ready_flow(self, pm_env):
        mod, broker, _ = pm_env
        mod.save_config({"url": "https://192.168.1.2:8006", "enabled": True})
        broker.store["proxmox_api_token_id"] = "nyra@pve!ui"
        broker.store["proxmox_api_token_secret"] = "secret-value-ui"
        settings = Settings(proxmox_enabled=True)
        client = StubProxmoxRuntime()
        services = make_pm_services(settings, client)

        result = await mod.test_connection(services)
        assert result["ok"] is True and result["state"] == "READY"
        status = mod.public_status(services)
        assert status["state"] == "READY" and status["authenticated"] is True

    async def test_test_connection_success_and_inventory_counts(self, pm_env):
        mod, broker, _ = pm_env
        mod.save_config({"url": "https://192.168.1.2:8006", "enabled": True})
        broker.store["proxmox_api_token_id"] = "nyra@pve!ui"
        broker.store["proxmox_api_token_secret"] = "secret-value-ui"
        settings = Settings(proxmox_enabled=True)
        client = StubProxmoxRuntime()
        services = make_pm_services(settings, client)

        result = await mod.test_connection(services)
        assert result["ok"] is True and result["state"] == "READY"
        assert result["qemu_count"] == 2 and result["lxc_count"] == 1
        assert result["storage_count"] == 1
        assert client.creds is not None  # runtime aplicado

        status = mod.public_status(services)
        assert status["state"] == "READY"
        assert status["authenticated"] is True
        serialized = json.dumps(status)
        assert "secret-value-ui" not in serialized  # §31: 0 leaks

    async def test_test_connection_auth_failed_recorded(self, pm_env):
        mod, broker, _ = pm_env
        mod.save_config({"url": "https://192.168.1.2:8006", "enabled": True})
        broker.store["proxmox_api_token_id"] = "nyra@pve!ui"
        broker.store["proxmox_api_token_secret"] = "wrong-secret"
        settings = Settings(proxmox_enabled=True)
        client = StubProxmoxRuntime()
        client.fail_with = IntegrationError("PROXMOX_AUTH_FAILED", "recusada")
        services = make_pm_services(settings, client)

        result = await mod.test_connection(services)
        assert result["state"] == "AUTH_FAILED"
        status = mod.public_status(services)
        assert status["state"] == "AUTH_FAILED"  # §34: token incorreto

    async def test_test_connection_offline(self, pm_env):
        import httpx as _httpx

        mod, broker, _ = pm_env
        mod.save_config({"url": "https://10.255.255.1:8006", "enabled": True})
        broker.store["proxmox_api_token_id"] = "id"
        broker.store["proxmox_api_token_secret"] = "sec"
        settings = Settings(proxmox_enabled=True)
        client = StubProxmoxRuntime()

        async def unreachable():
            raise _httpx.ConnectError("no route")

        client.fail_with = None
        client.version = unreachable  # type: ignore[method-assign]
        services = make_pm_services(settings, client)
        result = await mod.test_connection(services)
        assert result["state"] == "OFFLINE"

    async def test_inventory_splits_qemu_lxc_storage(self, pm_env):
        mod, broker, _ = pm_env
        settings = Settings(proxmox_enabled=True)
        client = StubProxmoxRuntime()
        services = make_pm_services(settings, client)
        inv = await mod.inventory(services)
        assert {g["vmid"] for g in inv["qemu"]} == {103, 120}
        assert {g["vmid"] for g in inv["lxc"]} == {201}
        assert inv["nodes"][0]["node"] == "pve"
        assert inv["storage"][0]["usage_percent"] == 25.0

    def test_disconnect_clears_broker_only(self, pm_env):
        mod, broker, _ = pm_env
        broker.store["proxmox_api_token_id"] = "id"
        broker.store["proxmox_api_token_secret"] = "sec"
        removed = mod.disconnect_credentials()
        assert removed["token_id_removed"] and removed["token_secret_removed"]
        assert broker.store == {}

    def test_apply_to_runtime_updates_both_clients(self, pm_env):
        mod, broker, _ = pm_env
        mod.save_config({"url": "https://192.168.9.9:8006", "enabled": True,
                         "verify_ssl": False})
        broker.store["proxmox_api_token_id"] = "op@pve!x"
        broker.store["proxmox_api_token_secret"] = "s3cret!"
        settings = Settings(proxmox_enabled=True)
        client_a = StubProxmoxRuntime()
        client_b = StubProxmoxRuntime()
        services = type("S", (), {"settings": settings,
                                  "proxmox": client_a,
                                  "homelab": StubController(client_b)})()
        summary = mod.apply_to_runtime(services)
        assert summary["applied"] is True
        assert client_a.creds[0] == "https://192.168.9.9:8006"
        assert client_b.creds[1] == "op@pve!x"
        assert client_b.creds[2] == "s3cret!"
        assert client_a.creds[3] is False  # verify_ssl propagado
        assert client_b.creds[3] is False

    def test_persistence_survives_restart(self, pm_env):
        """§59: campos não secretos persistem e são recarregados."""
        mod, _, _ = pm_env
        mod.save_config({"url": "https://192.168.1.2:8006", "enabled": True,
                         "preferred_node": "pve", "timeout_seconds": 12})
        config = mod.load_config(Settings())
        assert config["url"] == "https://192.168.1.2:8006"
        assert config["preferred_node"] == "pve"
        assert config["timeout_seconds"] == 12.0
        assert config["enabled"] is True

    def test_legacy_settings_tokens_migrate_to_broker(self, pm_env):
        mod, broker, _ = pm_env
        settings = Settings(proxmox_enabled=True,
                            proxmox_token_id="leg@pve!id",
                            proxmox_token_secret="NYRA_SECRET_LEAK_TEST_legacy")
        token_id, token_secret = mod.resolve_credentials(settings)
        assert token_id == "leg@pve!id"
        assert token_secret == "NYRA_SECRET_LEAK_TEST_legacy"
        # migrado silenciosamente:
        assert broker.store["proxmox_api_token_id"] == "leg@pve!id"
        assert broker.store["proxmox_api_token_secret"] == "NYRA_SECRET_LEAK_TEST_legacy"


# ============================================================== rotas HTTP


class EntitiesHomelab:
    async def ha_list_entities(self, domain=None, state=None, search=None, limit=25):
        return [{
            "entity_id": "light.sala", "state": "on",
            "friendly_name": "Lâmpada Sala", "domain": "light",
            "last_changed": "2026-08-24T00:00:00+00:00",
            "last_updated": "2026-08-24T00:00:00+00:00",
        }]

    async def ha_get_state(self, entity_id: str):
        if entity_id != "light.sala":
            raise IntegrationError("HA_ENTITY_NOT_FOUND", "não encontrado")
        return {"entity_id": "light.sala", "state": "on",
                "attributes": {"friendly_name": "Lâmpada Sala"},
                "last_changed": "", "last_updated": ""}


class ApprovalRequiredController:
    async def proxmox_vm_action(self, action, reference, *, approval_id=None, reason=""):
        return {"success": False, "error_code": "APPROVAL_REQUIRED",
                "approval_required": True, "approval_id": "ap-123",
                "risk_level": "ELEVATED"}


def build_route_client(services):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import router

    app = FastAPI()
    app.include_router(router)
    app.state.services = services
    return TestClient(app)


class TestRoutesV11:
    def _pm_patched(self, tmp_path, monkeypatch, broker=None):
        from app.integrations.proxmox import config as pm_mod

        monkeypatch.setattr(pm_mod, "CONFIG_PATH", tmp_path / "proxmox-config.json")
        monkeypatch.setattr(pm_mod, "load_runtime_settings", lambda: {})
        broker = broker or StubBroker()
        monkeypatch.setattr(pm_mod, "_broker", lambda: broker)
        return pm_mod, broker

    def test_proxmox_config_get_masks_secrets(self, tmp_path, monkeypatch):
        _, broker = self._pm_patched(tmp_path, monkeypatch)
        broker.store["proxmox_api_token_id"] = "id-x"
        broker.store["proxmox_api_token_secret"] = "NYRA_SECRET_LEAK_TEST_route"
        settings = Settings(proxmox_enabled=True,
                            proxmox_url="https://192.168.1.2:8006")
        client = build_route_client(type("S", (), {
            "settings": settings, "proxmox": None, "homelab": None})())
        response = client.get("/api/proxmox/config")
        assert response.status_code == 200
        payload = response.json()
        assert payload["token_id_configured"] is True
        assert payload["token_secret_configured"] is True
        assert "NYRA_SECRET_LEAK_TEST_route" not in response.text

    def test_proxmox_config_put_saves_and_applies(self, tmp_path, monkeypatch):
        _, broker = self._pm_patched(tmp_path, monkeypatch)
        runtime_client = StubProxmoxRuntime()
        settings = Settings(proxmox_enabled=True)
        client = build_route_client(make_pm_services(settings, runtime_client))
        response = client.put("/api/proxmox/config", json={
            "url": "https://192.168.1.2:8006", "enabled": True,
            "token_id": "op@pve!ui", "token_secret": "pair-secret-ui",
        })
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["runtime_applied"]["auth_configured"] is True
        assert runtime_client.creds is not None
        assert "pair-secret-ui" not in response.text

    def test_proxmox_config_put_rejects_partial_pair(self, tmp_path, monkeypatch):
        self._pm_patched(tmp_path, monkeypatch)
        client = build_route_client(make_pm_services(Settings(), StubProxmoxRuntime()))
        response = client.put("/api/proxmox/config", json={"token_id": "only-id"})
        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "PROXMOX_TOKEN_PAIR_REQUIRED"

    def test_proxmox_disconnect(self, tmp_path, monkeypatch):
        _, broker = self._pm_patched(tmp_path, monkeypatch)
        broker.store["proxmox_api_token_id"] = "id"
        broker.store["proxmox_api_token_secret"] = "sec"
        runtime_client = StubProxmoxRuntime()
        client = build_route_client(make_pm_services(Settings(proxmox_enabled=True),
                                                     runtime_client))
        response = client.post("/api/proxmox/disconnect")
        assert response.status_code == 200
        assert broker.store == {}

    def test_ha_entities_disabled_conflict(self, tmp_path, monkeypatch):
        client = build_route_client(type("S", (), {
            "settings": Settings(home_assistant_enabled=False),
            "homelab": EntitiesHomelab()})())
        response = client.get("/api/home-assistant/entities")
        assert response.status_code == 409

    def test_ha_entities_list_and_detail(self):
        client = build_route_client(type("S", (), {
            "settings": Settings(home_assistant_enabled=True),
            "homelab": EntitiesHomelab()})())
        listing = client.get("/api/home-assistant/entities").json()
        assert listing["count"] == 1
        assert listing["entities"][0]["domain"] == "light"
        detail = client.get("/api/home-assistant/entities/light.sala").json()
        assert detail["supported_services"] == ["turn_on", "turn_off", "toggle"]
        assert detail["safe_attributes"]["friendly_name"] == "Lâmpada Sala"

    def test_ha_entity_service_invalid_domain_422(self):
        client = build_route_client(type("S", (), {
            "settings": Settings(home_assistant_enabled=True),
            "homelab": EntitiesHomelab()})())
        response = client.post("/api/home-assistant/entities/bad!name/service",
                               json={"service": "toggle"})
        assert response.status_code == 422

    def test_power_action_returns_approval_envelope(self):
        """§37/§38: power op passa pelo executor real; approval single-use."""
        client = build_route_client(type("S", (), {
            "settings": Settings(),
            "homelab": ApprovalRequiredController()})())
        response = client.post("/api/homelab/proxmox/guests/120/action",
                               json={"action": "shutdown"})
        assert response.status_code == 202
        body = response.json()
        assert body["approval_required"] is True
        assert body["approval_id"] == "ap-123"
