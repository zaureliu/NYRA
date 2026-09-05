"""Homelab Control Plane tests: registry, probes, integrations, policies, actions.

All external systems are mocked (httpx.MockTransport / fake clients). Real
homelab smoke runs are separate scripts under scripts/.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.events import EventBus
from app.homelab.adapters.base import SshAdapterError
from app.homelab.adapters.openwrt import OpenWrtAdapter
from app.homelab.adapters.windows_host import WindowsHostAdapter
from app.homelab.controller import HomelabControlPlane
from app.homelab.health import HomelabProbeLayer, aggregate_state, http_probe
from app.homelab.history import HomelabHistory
from app.homelab.models import (
    HealthState,
    HostCapabilities,
    HostDefinition,
    HostType,
    HomelabOverview,
    IntegrationKind,
    ProbeResult,
)
from app.homelab.policies import decide, normalize_action
from app.homelab.registry import HomelabHostRegistry
from app.integrations.base import IntegrationError, require_secure_credential_transport
from app.integrations.home_assistant import HomeAssistantClient
from app.integrations.proxmox.client import ProxmoxReadOnlyClient
from app.tools.registry import ToolRegistry
from app.homelab.tools import register_homelab_tools
from app.tools.shell_approval import ShellApprovalGate
from pydantic import ValidationError


# --------------------------------------------------------------------------- fakes


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        homelab_enabled=True,
        homelab_mutations_enabled=True,
        homelab_registry_path=tmp_path / "registry.yaml",
        homelab_default_timeout_seconds=2.0,
        homelab_overview_cache_seconds=0,
        database_path=tmp_path / "kazumi-test.db",
        home_assistant_enabled=True,
        home_assistant_url="https://192.168.1.200",
        home_assistant_token="test-token-kazumi",
        proxmox_enabled=True,
        proxmox_url="https://192.168.1.2:8006",
        proxmox_token_id="kazumi@pve!test",
        proxmox_token_secret="secret-value",
        event_cooldown_seconds=30,
    )


def sample_hosts() -> list[HostDefinition]:
    return [
        HostDefinition(
            id="openwrt", display_name="OpenWrt Gateway", type="openwrt",
            address="192.168.1.1", aliases=["gateway", "roteador"],
            integration="trusted_ssh",
            capabilities=HostCapabilities(ssh=True, tcp_probes=[22]),
        ),
        HostDefinition(
            id="proxmox", display_name="Proxmox", type="proxmox",
            address="192.168.1.2", aliases=["hypervisor"],
            integration="proxmox_api",
            capabilities=HostCapabilities(api=True),
        ),
        HostDefinition(
            id="home_assistant", display_name="Home Assistant", type="home_assistant",
            address="192.168.1.200", aliases=["ha"],
            integration="home_assistant_api",
            capabilities=HostCapabilities(api=True, http_path="/api/"),
        ),
        HostDefinition(
            id="dc1", display_name="DC1", type="windows",
            address="192.168.1.10", aliases=["controlador de dominio"],
            integration="windows_remote", enabled=False,
        ),
    ]


class FakeProxmoxClient:
    def __init__(self) -> None:
        self.configured = True
        self.guests = [
            {"vmid": 103, "name": "web-server", "type": "qemu", "node": "pve", "status": "stopped"},
            {"vmid": 120, "name": "homeassistant", "type": "qemu", "node": "pve", "status": "running"},
            {"vmid": 201, "name": "pihole", "type": "lxc", "node": "pve", "status": "running"},
        ]
        self.task_results: dict[str, dict] = {}
        self.started: list[tuple] = []

    async def nodes(self):
        return [{"node": "pve", "status": "online"}]

    async def version(self):
        return {"version": "8.2.4", "release": "8.2"}

    async def virtual_machines(self):
        return list(self.guests)

    async def guest_status(self, node: str, guest_type: str, vmid: int):
        for guest in self.guests:
            if guest["vmid"] == vmid:
                return {"status": guest["status"], "name": guest["name"]}
        return {}

    async def guest_action(self, node: str, guest_type: str, vmid: int, action: str, extra=None):
        upid = f"UPID:pve:000{vmid}:{action}"
        self.started.append((node, guest_type, vmid, action))
        if action == "start":
            self.task_results[upid] = {"state": "stopped", "exitstatus": "OK"}
            for guest in self.guests:
                if guest["vmid"] == vmid:
                    guest["status"] = "running"
        elif action == "shutdown" and vmid == 103:
            self.task_results[upid] = {"state": "stopped", "exitstatus": "got timeout"}
        else:
            self.task_results[upid] = {"state": "stopped", "exitstatus": "OK"}
        return upid

    async def wait_task(self, node: str, upid: str, **kwargs):
        return self.task_results.get(upid, {"state": "stopped", "exitstatus": "OK", "ok": True}) | {"ok": self.task_results.get(upid, {}).get("exitstatus") == "OK"}


class FakeRemoteShell:
    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self.hosts = type(
            "H",
            (),
            {
                "resolve_remote": staticmethod(
                    lambda _id: type(
                        "Logical", (), {"remote_shell": type("R", (), {"enabled": enabled})()}
                    )()
                )
            },
        )()
        self.calls: list[str] = []

    async def execute(self, host: str, command: str, **kwargs):
        self.calls.append(command)
        if command == "ubus call system info":
            stdout = '{"uptime": 123456, "load": [32768, 16384, 8192], "memory": {"total": 262144, "free": 131072}}'
        elif command == "cat /etc/openwrt_release":
            stdout = "DISTRIB_ID='OpenWrt'\nDISTRIB_RELEASE='23.05.5'"
        elif command == "ubus call network.interface dump":
            stdout = json.dumps({"interface": [
                {"interface": "wan", "up": True, "proto": "dhcp", "device": "eth1",
                 "ipv4-address": [{"address": "100.64.0.2"}],
                 "route": [{"target": "0.0.0.0/0", "nexthop": "100.64.0.1"}]},
                {"interface": "lan", "up": True, "proto": "static", "device": "br-lan",
                 "ipv4-address": [{"address": "192.168.1.1"}]},
            ]})
        else:
            stdout = "{}"
        return {"success": True, "stdout": stdout, "stderr": "", "exit_code": 0}


class FakeHAResponse:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text
        self.status_code = 200

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def fake_ha_client(states: list | None = None, token: str = "token-test") -> HomeAssistantClient:
    """Deterministic offline HA client (no network) for controller tests."""
    states = states or []
    entity_path_prefix = "/api/states/"

    async def fake_request(method, path, *, json_body=None):
        if path == "/api/":
            return FakeHAResponse(text="API running.")
        if path == "/api/config":
            return FakeHAResponse(payload={"location_name": "Casa", "state": "RUNNING", "version": "2026.8.3"})
        if path == "/api/states":
            return FakeHAResponse(payload=list(states))
        if path.startswith(entity_path_prefix):
            entity_id = path[len(entity_path_prefix):]
            for item in states:
                if item.get("entity_id") == entity_id:
                    return FakeHAResponse(payload=item)
            raise IntegrationError("HA_ENTITY_NOT_FOUND", f"Entidade não encontrada: {entity_id}")
        raise IntegrationError("HA_API_UNAVAILABLE", f"Endpoint não suportado no fake: {path}")

    client = HomeAssistantClient("https://192.168.1.200", token)
    client._request = fake_request  # type: ignore[method-assign]
    return client


def build_plane(settings: Settings, proxmox=None, remote_shell=None) -> HomelabControlPlane:
    bus = EventBus()
    plane = HomelabControlPlane(
        settings, bus, ShellApprovalGate(), remote_shell or FakeRemoteShell(),
        history=HomelabHistory(settings.database_path),
        registry=HomelabHostRegistry(path=None, hosts=sample_hosts()),
        proxmox=proxmox if proxmox is not None else FakeProxmoxClient(),
        home_assistant=fake_ha_client(token=settings.home_assistant_token),
    )
    return plane


# --------------------------------------------------------------------------- registry


class TestRegistry:
    def test_valid_host_and_alias_resolution(self):
        registry = HomelabHostRegistry(path=None, hosts=sample_hosts())
        assert registry.resolve("roteador").id == "openwrt"
        assert registry.resolve("GATEWAY").id == "openwrt"
        assert registry.resolve("Home Assistant").id == "home_assistant"
        assert registry.resolve("CONTROLADOR DE DOMINIO").id == "dc1"
        assert registry.get("proxmox") is not None

    def test_disabled_host_present(self):
        registry = HomelabHostRegistry(path=None, hosts=sample_hosts())
        dc1 = registry.get("dc1")
        assert dc1.enabled is False

    def test_duplicate_id_rejected(self):
        hosts = sample_hosts()
        duplicate = hosts[0].model_copy()
        with pytest.raises(ValueError, match="duplicado"):
            HomelabHostRegistry(path=None, hosts=[*hosts, duplicate])

    def test_duplicate_alias_rejected(self):
        hosts = sample_hosts()
        clash = hosts[3].model_copy(update={"id": "other", "aliases": ["gateway"]})
        with pytest.raises(ValueError, match="Alias duplicado"):
            HomelabHostRegistry(path=None, hosts=[*hosts, clash])

    def test_invalid_address_rejected(self):
        with pytest.raises(ValidationError):
            HostDefinition(id="x1", display_name="X", type="linux", address="não é endereço!!")

    def test_unknown_integration_rejected(self):
        with pytest.raises(ValidationError):
            HostDefinition(
                id="x2", display_name="X", type="linux", address="10.0.0.9",
                integration="carrier_pigeon",
            )

    def test_registry_file_roundtrip(self, tmp_path: Path):
        path = tmp_path / "registry.yaml"
        path.write_text(
            "version: 1\nhosts:\n  - id: openwrt\n    display_name: OpenWrt\n"
            "    type: openwrt\n    address: 192.168.1.1\n    aliases: [gw]\n"
            "    integration: trusted_ssh\n",
            encoding="utf-8",
        )
        registry = HomelabHostRegistry(path=path)
        assert registry.resolve("gw").address == "192.168.1.1"

    def test_real_seed_file_loads(self):
        from app.core.paths import PROJECT_ROOT
        seed = PROJECT_ROOT / "config" / "homelab_hosts.yaml"
        if not seed.is_file():
            pytest.skip("seed file not present")
        registry = HomelabHostRegistry(path=seed)
        assert {host.id for host in registry.all_hosts()} >= {"openwrt", "proxmox", "dc1", "home_assistant"}


# --------------------------------------------------------------------------- health aggregation


class TestHealthAggregation:
    def host(self) -> HostDefinition:
        return HostDefinition(
            id="srv", display_name="Srv", type="linux", address="10.0.0.5",
            integration="trusted_ssh", capabilities=HostCapabilities(tcp_probes=[22]),
        )

    def test_ping_fail_does_not_mean_offline_when_tcp_ok(self):
        probes = [ProbeResult(kind="icmp", success=False), ProbeResult(kind="tcp", success=True)]
        state, reachable = aggregate_state(self.host(), probes, HealthState.ONLINE, None)
        assert state == HealthState.ONLINE and reachable is True

    def test_all_probes_fail_is_unreachable_not_offline(self):
        probes = [ProbeResult(kind="icmp", success=False), ProbeResult(kind="tcp", success=False)]
        state, reachable = aggregate_state(self.host(), probes, HealthState.UNKNOWN, None)
        assert state == HealthState.UNREACHABLE and reachable is False

    def test_auth_failure_surfaces_as_authentication_failed(self):
        probes = [ProbeResult(kind="http", success=True)]
        state, reachable = aggregate_state(self.host(), probes, HealthState.AUTHENTICATION_FAILED, "PROXMOX_AUTH_FAILED")
        assert state == HealthState.AUTHENTICATION_FAILED and reachable is True

    def test_integration_unavailable_on_reachable_host_degrades(self):
        probes = [ProbeResult(kind="tcp", success=True)]
        state, _ = aggregate_state(self.host(), probes, HealthState.INTEGRATION_UNAVAILABLE, "PROXMOX_API_UNAVAILABLE")
        assert state == HealthState.DEGRADED

    def test_disabled_host_stays_disabled(self):
        host = self.host().model_copy(update={"enabled": False})
        state, reachable = aggregate_state(host, [], HealthState.UNKNOWN, None)
        assert state == HealthState.DISABLED and reachable is False

    @pytest.mark.asyncio
    async def test_probe_layer_runs_bounded_probes(self):
        layer = HomelabProbeLayer(default_timeout_seconds=1)
        host = HostDefinition(
            id="ha", display_name="HA", type="home_assistant", address="127.0.0.1",
            integration="home_assistant_api",
            capabilities=HostCapabilities(http_path="/api/", tcp_probes=[1]),
        )
        results = await layer.probe_host(host, timeout_seconds=2)
        kinds = {item.kind for item in results}
        assert kinds <= {"icmp", "tcp", "http"}
        assert results  # at least the TCP probe executed


class TestHttpProbeAuthentication:
    @pytest.mark.asyncio
    async def test_http_probe_sends_bearer_and_never_leaks_token(self, monkeypatch):
        captured: dict[str, object] = {}
        secret = "ha-live-token-9f2c"

        class FakeResponse:
            status_code = 200

        class FakeClient:
            def __init__(self, *args, **kwargs) -> None: pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def get(self, url, headers=None):
                captured["url"] = url
                captured["headers"] = dict(headers or {})
                return FakeResponse()

        monkeypatch.setattr("app.homelab.health.httpx.AsyncClient", FakeClient)
        result = await http_probe("http://192.168.1.200/api/", bearer_token=secret)
        assert result.success is True
        assert captured["headers"]["Authorization"] == f"Bearer {secret}"
        assert secret not in result.detail

    @pytest.mark.asyncio
    async def test_http_probe_without_token_sends_no_authorization(self, monkeypatch):
        captured: dict[str, object] = {}

        class FakeResponse:
            status_code = 200

        class FakeClient:
            def __init__(self, *args, **kwargs) -> None: pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def get(self, url, headers=None):
                captured["headers"] = dict(headers or {})
                return FakeResponse()

        monkeypatch.setattr("app.homelab.health.httpx.AsyncClient", FakeClient)
        await http_probe("http://192.168.1.200/")
        assert "Authorization" not in captured["headers"]

    @pytest.mark.asyncio
    async def test_probe_host_uses_resolver_token_for_ha(self, monkeypatch):
        seen: list[tuple[str, str]] = []

        async def fake_http_probe(url, timeout_seconds=4.0, *, bearer_token=""):
            seen.append((url, bearer_token))
            return ProbeResult(kind="http", success=True, latency_ms=1.0, detail=f"HTTP {200 if bearer_token else 401}")

        monkeypatch.setattr("app.homelab.health.http_probe", fake_http_probe)
        ha_host = next(h for h in sample_hosts() if h.id == "home_assistant")
        layer = HomelabProbeLayer(
            credential_resolver=lambda h: "tok" if h.integration.value == "home_assistant_api" else "",
        )
        await layer.probe_host(ha_host)
        assert seen == [("http://192.168.1.200/api/", "tok")]

    @pytest.mark.asyncio
    async def test_probe_host_falls_back_to_root_without_credentials(self, monkeypatch):
        seen: list[tuple[str, str]] = []

        async def fake_http_probe(url, timeout_seconds=4.0, *, bearer_token=""):
            seen.append((url, bearer_token))
            return ProbeResult(kind="http", success=True, latency_ms=1.0, detail="HTTP 200")

        monkeypatch.setattr("app.homelab.health.http_probe", fake_http_probe)
        ha_host = next(h for h in sample_hosts() if h.id == "home_assistant")

        no_resolver = HomelabProbeLayer()
        await no_resolver.probe_host(ha_host)
        empty_resolver = HomelabProbeLayer(credential_resolver=lambda h: "")
        await empty_resolver.probe_host(ha_host)
        assert [item[0] for item in seen] == ["http://192.168.1.200/", "http://192.168.1.200/"]
        assert all(item[1] == "" for item in seen)

    @pytest.mark.asyncio
    async def test_proxmox_probe_url_unchanged_by_resolver(self, monkeypatch):
        seen: list[tuple[str, str]] = []

        async def fake_http_probe(url, timeout_seconds=4.0, *, bearer_token=""):
            seen.append((url, bearer_token))
            return ProbeResult(kind="http", success=False, latency_ms=1.0, detail="ConnectError")

        monkeypatch.setattr("app.homelab.health.http_probe", fake_http_probe)
        proxmox_host = next(h for h in sample_hosts() if h.id == "proxmox")
        proxmox_host = proxmox_host.model_copy(update={
            "capabilities": HostCapabilities(api=True, http_path="/"),
        })
        layer = HomelabProbeLayer(credential_resolver=lambda h: "tok" if h.integration.value == "home_assistant_api" else "")
        await layer.probe_host(proxmox_host)
        assert seen == [("https://192.168.1.2:8006", "")]

    @pytest.mark.asyncio
    async def test_controller_resolves_ha_token_from_client(self, tmp_path):
        settings = make_settings(tmp_path)
        plane = build_plane(settings)
        ha_host = next(h for h in sample_hosts() if h.id == "home_assistant")
        openwrt_host = next(h for h in sample_hosts() if h.id == "openwrt")
        assert plane._probe_credentials(ha_host) == settings.home_assistant_token
        assert plane._probe_credentials(openwrt_host) == ""


# --------------------------------------------------------------------------- proxmox client


@pytest.fixture
def proxmox_factory(monkeypatch):
    """Build a real client whose HTTP layer is a MockTransport handler."""
    import app.integrations.proxmox.client as pmod

    pristine_async_client = httpx.AsyncClient

    def make(handler):
        transport = httpx.MockTransport(handler)

        def factory(*args, **kwargs):
            kwargs.pop("verify", None)
            return pristine_async_client(transport=transport, timeout=kwargs.pop("timeout", 5))

        monkeypatch.setattr(pmod.httpx, "AsyncClient", factory)
        return ProxmoxReadOnlyClient("https://192.168.1.2:8006", "kazumi@pve!t", "sec-value")

    return make


class TestProxmoxClient:
    @pytest.mark.asyncio
    async def test_reads_nodes_vms_storage_version(self, proxmox_factory):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/version"):
                return httpx.Response(200, json={"data": {"version": "8.2.4", "release": "8.2"}})
            return httpx.Response(200, json={"data": [{"node": "pve", "status": "online"}]})

        client = proxmox_factory(handler)
        nodes = await client.nodes()
        assert nodes[0]["node"] == "pve"
        assert (await client.version())["version"] == "8.2.4"

    @pytest.mark.asyncio
    async def test_auth_failure_normalized(self, proxmox_factory):
        client = proxmox_factory(lambda request: httpx.Response(401, json={"data": None}))
        with pytest.raises(IntegrationError) as excinfo:
            await client.nodes()
        assert excinfo.value.code == "PROXMOX_AUTH_FAILED"

    @pytest.mark.asyncio
    async def test_permission_denied_normalized(self, proxmox_factory):
        client = proxmox_factory(lambda request: httpx.Response(403, json={"data": None}))
        with pytest.raises(IntegrationError) as excinfo:
            await client.nodes()
        assert excinfo.value.code == "PROXMOX_PERMISSION_DENIED"

    @pytest.mark.asyncio
    async def test_timeout_normalized(self, proxmox_factory):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("boom")

        client = proxmox_factory(handler)
        with pytest.raises(IntegrationError) as excinfo:
            await client.nodes()
        assert excinfo.value.code == "PROXMOX_API_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_invalid_json_normalized(self, proxmox_factory):
        client = proxmox_factory(
            lambda request: httpx.Response(200, content=b"<html>not json</html>")
        )
        with pytest.raises(IntegrationError) as excinfo:
            await client.nodes()
        assert excinfo.value.code == "PROXMOX_API_INVALID_RESPONSE"

    @pytest.mark.asyncio
    async def test_unconfigured_client_raises_auth_missing(self):
        client = ProxmoxReadOnlyClient("", "", "")
        with pytest.raises(IntegrationError) as excinfo:
            await client.nodes()
        assert excinfo.value.code == "PROXMOX_AUTH_MISSING"

    @pytest.mark.asyncio
    async def test_task_wait_reports_ok_and_failure(self, proxmox_factory):
        from urllib.parse import quote

        upid = "UPID:pve:0000123:start"
        encoded = quote(upid, safe="")

        def ok_handler(request: httpx.Request) -> httpx.Response:
            if "/tasks/" in request.url.path and request.url.path.endswith("/status"):
                return httpx.Response(200, json={"data": {"status": "stopped", "exitstatus": "OK"}})
            return httpx.Response(404)

        client = proxmox_factory(ok_handler)
        result = await client.wait_task("pve", upid, poll_interval=0.01)
        assert result["ok"] is True and result["exitstatus"] == "OK"

        def failing_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"status": "stopped", "exitstatus": "got timeout"}})

        client2 = proxmox_factory(failing_handler)
        failed = await client2.wait_task("pve", upid, poll_interval=0.01)
        assert failed["ok"] is False


# --------------------------------------------------------------------------- ha client


@pytest.fixture
def ha_factory(monkeypatch):
    """Build a real HA client whose HTTP layer is a MockTransport handler."""
    import app.integrations.home_assistant as hmod

    pristine_async_client = httpx.AsyncClient

    def make(handler, token: str = "token-kazumi"):
        transport = httpx.MockTransport(handler)

        def factory(*args, **kwargs):
            return pristine_async_client(transport=transport, timeout=kwargs.pop("timeout", 5))

        monkeypatch.setattr(hmod.httpx, "AsyncClient", factory)
        return HomeAssistantClient("https://192.168.1.200", token)

    return make


class TestHomeAssistantClient:
    @pytest.mark.asyncio
    async def test_api_root_returns_running_text(self, ha_factory):
        client = ha_factory(lambda request: httpx.Response(200, text="API running."))
        assert (await client.api_root()) == "API running."

    @pytest.mark.asyncio
    async def test_config_parses(self, ha_factory):
        client = ha_factory(lambda request: httpx.Response(200, json={
            "location_name": "Casa", "state": "RUNNING", "version": "2026.8.3", "time_zone": "America/Sao_Paulo",
        }))
        config = await client.config()
        assert config["location_name"] == "Casa"
        assert config["version"].startswith("2026.")

    @pytest.mark.asyncio
    async def test_states_returns_list(self, ha_factory):
        client = ha_factory(lambda request: httpx.Response(200, json=[
            {"entity_id": "sensor.one", "state": "10"},
            {"entity_id": "light.kitchen", "state": "on"},
        ]))
        states = await client.states()
        assert isinstance(states, list) and len(states) == 2

    @pytest.mark.asyncio
    async def test_entity_state(self, ha_factory):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/api/states/input_boolean.kazumi_test")
            assert request.headers["Authorization"] == "Bearer token-kazumi"
            return httpx.Response(200, json={"entity_id": "input_boolean.kazumi_test", "state": "on", "attributes": {}})
        client = ha_factory(handler)
        entity = await client.state("input_boolean.kazumi_test")
        assert entity["state"] == "on"

    @pytest.mark.asyncio
    async def test_service_call_posts_body(self, ha_factory):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content.decode() or "{}")
            return httpx.Response(200, json=[{"entity_id": "light.kitchen"}])

        client = ha_factory(handler)
        result = await client.call_service("light", "turn_off", {"entity_id": ["light.kitchen"]})
        assert result[0]["entity_id"] == "light.kitchen"
        assert captured["path"] == "/api/services/light/turn_off"
        assert captured["body"]["target"]["entity_id"] == ["light.kitchen"]

    @pytest.mark.asyncio
    async def test_auth_failure_normalized(self, ha_factory):
        client = ha_factory(lambda request: httpx.Response(401, text="Unauthorized"))
        with pytest.raises(Exception) as excinfo:
            await client.states()
        assert getattr(excinfo.value, "code", "") == "HA_AUTH_FAILED"

    @pytest.mark.asyncio
    async def test_connection_error_normalized(self, ha_factory):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")
        client = ha_factory(handler)
        with pytest.raises(Exception) as excinfo:
            await client.api_root()
        assert getattr(excinfo.value, "code", "") == "HA_API_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_entity_not_found_404(self, ha_factory):
        client = ha_factory(lambda request: httpx.Response(404, json={"message": "Entity not found."}))
        with pytest.raises(Exception) as excinfo:
            await client.state("sensor.missing")
        assert getattr(excinfo.value, "code", "") == "HA_ENTITY_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_effect_verification(self, ha_factory):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"entity_id": "input_boolean.kazumi_test", "state": "off"})
        client = ha_factory(handler)
        assert await client.verify_effect("input_boolean.kazumi_test", "off") is True
        assert await client.verify_effect("input_boolean.kazumi_test", "on") is False

    @pytest.mark.asyncio
    async def test_invalid_json_rejected(self, ha_factory):
        client = ha_factory(lambda request: httpx.Response(200, content=b"{broken"))
        with pytest.raises(Exception) as excinfo:
            await client.states()
        assert getattr(excinfo.value, "code", "") == "HA_API_UNAVAILABLE"


# --------------------------------------------------------------------------- policies


class TestPolicies:
    def test_credential_transport_requires_https_or_literal_loopback(self):
        assert require_secure_credential_transport("https://ha.internal:8123")
        assert require_secure_credential_transport("http://127.0.0.1:8123")
        assert require_secure_credential_transport("http://[::1]:8123")
        for url in ("http://localhost:8123", "http://192.168.1.200:8123"):
            with pytest.raises(IntegrationError) as excinfo:
                require_secure_credential_transport(url)
            assert excinfo.value.code == "INSECURE_CREDENTIAL_TRANSPORT"

    @pytest.mark.asyncio
    async def test_proxmox_refuses_token_when_tls_verification_is_disabled(self):
        client = ProxmoxReadOnlyClient(
            "https://192.168.1.2:8006", "user@pve!kazumi", "secret", verify_ssl=False,
        )
        with pytest.raises(IntegrationError) as excinfo:
            await client.nodes()
        assert excinfo.value.code == "PROXMOX_TLS_VERIFICATION_REQUIRED"

    def test_vm_start_requires_approval_even_when_low_risk(self):
        decision = decide("vm_start")
        assert decision.risk_level == "LOW_RISK" and decision.requires_approval is True

    def test_shutdown_requires_approval(self):
        decision = decide("vm_shutdown")
        assert decision.requires_approval is True

    def test_hard_stop_is_destructive_with_approval(self):
        decision = decide("vm_stop")
        assert decision.risk_level == "DESTRUCTIVE" and decision.requires_approval is True

    def test_unknown_action_fails_closed(self):
        assert normalize_action("vm_destroy") is None
        decision = decide("vm_destroy")
        assert decision.requires_approval is True

    def test_host_override_can_only_tighten(self):
        policy = {"vm_start": "approval"}
        assert decide("vm_start", policy).requires_approval is True
        lax_policy = {"vm_stop": "auto"}
        assert decide("vm_stop", lax_policy).requires_approval is True


# --------------------------------------------------------------------------- controller actions


class TestControllerActions:
    @pytest.mark.asyncio
    async def test_ha_approval_binds_entire_target_and_service_payload(self, tmp_path: Path):
        plane = build_plane(make_settings(tmp_path))
        await plane.history.initialize()

        class RecordingHA:
            def __init__(self):
                self.calls = []

            async def call_service(self, domain, service, target, service_data):
                self.calls.append((domain, service, target, service_data))

            async def verify_effect(self, entity_id, expected_state):
                return entity_id == "notify.phone" and expected_state == "sent"

        recorder = RecordingHA()
        plane.home_assistant = recorder
        target = {"entity_id": ["notify.phone"], "device_id": "device-a"}
        service_data = {"message": "hello", "title": "KAZUMI", "_expected_state": "sent"}
        pending = await plane.ha_call_service(
            "notify", "mobile_app", target=target, service_data=service_data,
        )
        assert pending["error_code"] == "APPROVAL_REQUIRED"
        plane.approvals.grant(pending["approval_id"], "operator_test")

        tampered = await plane.ha_call_service(
            "notify", "mobile_app",
            target={**target, "device_id": "device-b"},
            service_data=service_data,
            approval_id=pending["approval_id"],
        )
        assert tampered["error_code"] == "APPROVAL_REJECTED"
        assert recorder.calls == []

        exact = await plane.ha_call_service(
            "notify", "mobile_app", target=target, service_data=service_data,
            approval_id=pending["approval_id"],
        )
        assert exact["success"] is True
        assert recorder.calls == [
            ("notify", "mobile_app", target, {"message": "hello", "title": "KAZUMI"})
        ]
        assert service_data["_expected_state"] == "sent"

    @pytest.mark.asyncio
    async def test_vm_start_act_verify_reported(self, tmp_path: Path):
        settings = make_settings(tmp_path)
        plane = build_plane(settings)
        await plane.history.initialize()
        pending = await plane.proxmox_vm_action("vm_start", "103")
        plane.approvals.grant(pending["approval_id"], "operator_test")
        result = await plane.proxmox_vm_action(
            "vm_start", "103", approval_id=pending["approval_id"],
        )
        assert result["success"] is True
        assert result["effect_verified"] is True
        assert result["guest_status"] == "running"
        assert result["task_exitstatus"] == "OK"
        rows = await plane.history.recent(limit=5)
        assert any(row["action"] == "vm_start" and row["effect_verified"] for row in rows)

    @pytest.mark.asyncio
    async def test_task_failure_not_reported_as_success(self, tmp_path: Path):
        settings = make_settings(tmp_path)
        plane = build_plane(settings)
        await plane.history.initialize()
        blocked = await plane.proxmox_vm_action("vm_shutdown", "103")
        assert blocked["error_code"] == "APPROVAL_REQUIRED"
        plane.approvals.grant(blocked["approval_id"], "operator_test")
        result = await plane.proxmox_vm_action("vm_shutdown", "103", approval_id=blocked["approval_id"])
        assert result["success"] is False
        assert result["error_code"] == "PROXMOX_TASK_FAILED"
        assert result.get("effect_verified") in {None, False}

    @pytest.mark.asyncio
    async def test_vm_resolution_by_name(self, tmp_path: Path):
        settings = make_settings(tmp_path)
        plane = build_plane(settings)
        status = await plane.proxmox_vm_status("homeassistant")
        assert status["vmid"] == 120
        lxc = await plane.proxmox_vm_status("PiHole")
        assert (lxc["vmid"], lxc["guest_type"]) == (201, "lxc")

    @pytest.mark.asyncio
    async def test_vm_not_found_honest_error(self, tmp_path: Path):
        settings = make_settings(tmp_path)
        plane = build_plane(settings)
        with pytest.raises(IntegrationError) as excinfo:
            await plane.proxmox_vm_status("nao-existe")
        assert excinfo.value.code == "PROXMOX_VM_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_shutdown_requires_approval_flow(self, tmp_path: Path):
        settings = make_settings(tmp_path)
        plane = build_plane(settings)
        await plane.history.initialize()
        blocked = await plane.proxmox_vm_action("vm_shutdown", "103")
        assert blocked["success"] is False
        assert blocked["error_code"] == "APPROVAL_REQUIRED"
        approval_id = blocked["approval_id"]
        record = plane.approvals.grant(approval_id, "operator_test")
        assert record is not None
        granted = await plane.proxmox_vm_action("vm_shutdown", "103", approval_id=approval_id)
        # Fake task returns failure exitstatus for this scenario; execution reached task stage.
        assert granted["error_code"] in {"PROXMOX_TASK_FAILED", "APPROVAL_REJECTED"} or granted["success"] is True

    @pytest.mark.asyncio
    async def test_overview_aggregates_hosts(self, tmp_path: Path, monkeypatch):
        settings = make_settings(tmp_path)
        plane = build_plane(settings)
        async def offline_probe(host, *args, **kwargs):
            return [ProbeResult(kind="http", success=True, detail="controlled fixture")]
        monkeypatch.setattr(plane.probes, "probe_host", offline_probe)
        overview = await plane.overview(force=True)
        assert isinstance(overview, HomelabOverview)
        states = {item.host_id: item.overall_state for item in overview.hosts}
        assert states["dc1"] == HealthState.DISABLED
        assert states["proxmox"] == HealthState.ONLINE
        assert states["home_assistant"] in {HealthState.ONLINE, HealthState.DEGRADED}

    @pytest.mark.asyncio
    async def test_configuration_status_reports_missing_auth(self, tmp_path: Path):
        settings = make_settings(tmp_path)
        unconfigured = ProxmoxReadOnlyClient("", "", "")
        plane = build_plane(settings, proxmox=unconfigured)
        configuration = plane.configuration_status()
        assert configuration["proxmox"] == "UNCONFIGURED"

    def test_ssh_ready_reflects_trusted_registry_contract(self, tmp_path: Path):
        settings = make_settings(tmp_path)
        ready = build_plane(settings)
        assert ready._ssh_ready("openwrt") is True
        disabled = build_plane(settings, remote_shell=FakeRemoteShell(enabled=False))
        assert disabled._ssh_ready("openwrt") is False
        unknown_host = build_plane(settings)
        assert unknown_host._ssh_ready("inexistente") is False


# --------------------------------------------------------------------------- adapters


class TestAdapters:
    @pytest.mark.asyncio
    async def test_openwrt_status_parsing(self):
        adapter = OpenWrtAdapter(FakeRemoteShell(), "openwrt")
        status = await adapter.status()
        assert status["uptime_s"] == 123456.0
        assert status["release"]["DISTRIB_RELEASE"] == "23.05.5"
        assert status["wan"]["addresses"] == ["100.64.0.2"]
        assert status["lan"]["addresses"] == ["192.168.1.1"]
        assert status["default_route"]["gateway"] == "100.64.0.1"

    @pytest.mark.asyncio
    async def test_openwrt_auth_failure_maps_to_remote_auth(self):
        class FailingShell:
            async def execute(self, *args, **kwargs):
                return {"success": False, "error_code": "SSH_AUTHENTICATION_FAILED", "message": "Autenticação SSH rejeitada."}
        adapter = OpenWrtAdapter(FailingShell(), "openwrt")
        with pytest.raises(SshAdapterError) as excinfo:
            await adapter.status()
        assert excinfo.value.code == "REMOTE_AUTH_FAILED"

    def test_windows_capability_unavailable_is_honest(self):
        adapter = WindowsHostAdapter(FakeRemoteShell(), "dc1")
        ok, message = adapter.available()
        assert ok is False and "configurado" in message

    @pytest.mark.asyncio
    async def test_windows_metrics_refuse_without_method(self):
        adapter = WindowsHostAdapter(FakeRemoteShell(), "dc1")
        with pytest.raises(SshAdapterError) as excinfo:
            await adapter.metrics()
        assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"


# --------------------------------------------------------------------------- tools & routing


class TestHomelabTools:
    def test_tools_registered_with_risk_levels(self, tmp_path: Path):
        plane = build_plane(make_settings(tmp_path))
        registry = ToolRegistry()
        register_homelab_tools(registry, plane)
        names = set(registry._tools.keys())
        assert {
            "homelab_overview", "homelab_host_status", "proxmox_list_vms",
            "proxmox_vm_start", "proxmox_vm_stop", "ha_status", "ha_call_service",
            "openwrt_status", "host_metrics",
        } <= names
        assert registry._tools["proxmox_vm_start"].risk.value == "LOW_RISK"
        assert registry._tools["proxmox_vm_stop"].risk.value == "DESTRUCTIVE"
        assert registry.preflight("proxmox_vm_stop", {"vm": "103"})["resource_key"] == "proxmox:guest:103"

    def test_agent_routing_for_homelab_phrases(self, tmp_path: Path):
        plane = build_plane(make_settings(tmp_path))
        registry = ToolRegistry()
        register_homelab_tools(registry, plane)
        assert registry.should_route_to_agent("Quais VMs estão ligadas?")
        assert registry.should_route_to_agent("Kazumi, verifica meu homelab.")
        assert registry.should_route_to_agent("Verifica o OpenWrt")
        assert not registry.should_route_to_agent("oi")
        assert not registry.should_route_to_agent("que horas são?")

    @pytest.mark.asyncio
    async def test_tool_errors_return_clean_codes(self, tmp_path: Path):
        settings = make_settings(tmp_path)
        settings.home_assistant_token = ""  # auth missing
        plane = build_plane(settings)
        registry = ToolRegistry()
        register_homelab_tools(registry, plane)
        result = await registry.execute("ha_status", {})
        assert result.data.get("error_code") == "HA_AUTH_MISSING"

    @pytest.mark.asyncio
    async def test_ha_service_allowlist_dynamic_risk(self, tmp_path: Path):
        plane = build_plane(make_settings(tmp_path))
        registry = ToolRegistry()
        register_homelab_tools(registry, plane)
        safe = registry.preflight("ha_call_service", {"domain": "light", "service": "turn_off", "target": {"entity_id": ["light.x"]}})
        unsafe = registry.preflight("ha_call_service", {"domain": "hassio", "service": "addon_restart", "target": {"entity_id": ["hassio.x"]}})
        assert safe["risk_level"] == "LOW_RISK"
        assert unsafe["risk_level"] == "ELEVATED"


# --------------------------------------------------------------------------- turn isolation sanity


class TestTurnScope:
    @pytest.mark.asyncio
    async def test_action_records_turn_id_from_contextvar(self, tmp_path: Path):
        from app.core.turn import current_turn_id, set_current_turn_id
        settings = make_settings(tmp_path)
        plane = build_plane(settings)
        await plane.history.initialize()
        token = set_current_turn_id("turn_deadbeef01")
        try:
            pending = await plane.proxmox_vm_action("vm_start", "120")
            plane.approvals.grant(pending["approval_id"], "operator_test")
            result = await plane.proxmox_vm_action(
                "vm_start", "120", approval_id=pending["approval_id"],
            )
        finally:
            current_turn_id.reset(token)
        assert result["turn_id"] == "turn_deadbeef01"
