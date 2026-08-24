"""Hotfix openwrt_config_hotfix — configuração OpenWrt via UI.

Cobre:
    * senha SSH exclusivamente no Credential Broker (0 leaks no status);
    * migração silenciosa da senha legada (.env/settings);
    * estados coerentes: UNCONFIGURED/AUTH_MISSING, AUTH_FAILED, OFFLINE,
      READY, DISABLED;
    * Test Connection usa o caminho do adapter existente
      (services.homelab.openwrt_status → OpenWrtAdapter);
    * persistência não secreta sobrevive a restart;
    * rotas GET/PUT/POST com mascaramento de secrets.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.homelab.adapters.base import SshAdapterError  # noqa: E402


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


class StubHomelab:
    """Control plane mínimo: mesmo contrato de openwrt_status()."""

    def __init__(self, fail_with: Exception | None = None,
                 payload: dict | None = None) -> None:
        self.fail_with = fail_with
        self.payload = payload or {
            "uptime_s": 1200.0,
            "release": {"DISTRIB_DESCRIPTION": "OpenWrt 23.05.3"},
        }
        self.calls = 0

    async def openwrt_status(self) -> dict:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return dict(self.payload)


@pytest.fixture()
def ow_env(tmp_path, monkeypatch):
    from app.integrations.openwrt import config as mod

    monkeypatch.setattr(mod, "CONFIG_PATH", tmp_path / "openwrt-config.json")
    monkeypatch.setattr(mod, "load_runtime_settings", lambda: {})
    broker = StubBroker()
    monkeypatch.setattr(mod, "_broker", lambda: broker)
    yield mod, broker, tmp_path


def make_ow_services(settings: Settings, homelab: StubHomelab | None = None):
    return type("S", (), {"settings": settings, "homelab": homelab})()


# ============================================================== config/broker


class TestOpenWrtConfigPersistence:
    def test_save_and_load_roundtrip(self, ow_env):
        mod, _, _ = ow_env
        mod.save_config({"url": "http://192.168.1.1", "username": "root"})
        config = mod.load_config(Settings())
        assert config["url"] == "http://192.168.1.1"
        assert config["username"] == "root"

    def test_persistence_survives_restart(self, ow_env):
        """§59: campos não secretos persistem e são recarregados."""
        mod, _, _ = ow_env
        mod.save_config({"url": "http://192.168.1.1", "username": "nyra"})
        config = mod.load_config(Settings(openwrt_url="", openwrt_username=""))
        assert config["url"] == "http://192.168.1.1"
        assert config["username"] == "nyra"

    def test_partial_save_never_wipes_saved_fields(self, ow_env):
        mod, _, _ = ow_env
        mod.save_config({"url": "http://192.168.1.1", "username": "nyra"})
        mod.save_config({"username": "root"})
        config = mod.load_config(Settings())
        assert config["url"] == "http://192.168.1.1"  # preservado
        assert config["username"] == "root"


class TestOpenWrtCredentials:
    def test_legacy_settings_password_migrates_to_broker(self, ow_env):
        mod, broker, _ = ow_env
        settings = Settings(openwrt_password="NYRA_SECRET_LEAK_TEST_ow")
        resolved = mod.resolve_password(settings)
        assert resolved == "NYRA_SECRET_LEAK_TEST_ow"
        assert broker.store["openwrt_ssh_password"] == "NYRA_SECRET_LEAK_TEST_ow"

    def test_broker_password_wins_over_legacy(self, ow_env):
        mod, broker, _ = ow_env
        broker.store["openwrt_ssh_password"] = "senha-do-broker"
        assert mod.resolve_password(Settings(openwrt_password="legado")) == "senha-do-broker"

    def test_save_credentials_requires_nonempty(self, ow_env):
        mod, _, _ = ow_env
        with pytest.raises(ValueError):
            mod.save_credentials("   ")

    def test_public_status_zero_leak(self, ow_env):
        mod, broker, _ = ow_env
        mod.save_config({"url": "http://192.168.1.1"})
        broker.store["openwrt_ssh_password"] = "NYRA_SECRET_LEAK_TEST_ui"
        status = mod.public_status(make_ow_services(Settings()))
        serialized = json.dumps(status, default=str)
        assert "NYRA_SECRET_LEAK_TEST_ui" not in serialized
        assert status["auth_configured"] is True
        assert status["password_configured"] is True


# ==================================================================== estados


class TestOpenWrtStates:
    def test_unconfigured_without_url_or_password(self, ow_env):
        """§7: sem credencial → UNCONFIGURED/AUTH_MISSING, nunca READY."""
        mod, _, _ = ow_env
        status = mod.public_status(make_ow_services(Settings()))
        assert status["state"] == "UNCONFIGURED"

    def test_unconfigured_when_url_without_password(self, ow_env):
        mod, _, _ = ow_env
        mod.save_config({"url": "http://192.168.1.1"})
        status = mod.public_status(make_ow_services(Settings()))
        assert status["state"] == "UNCONFIGURED"
        assert "AUTH_MISSING" in status["health"]

    def test_disabled_with_homelab_off(self, ow_env):
        mod, broker, _ = ow_env
        mod.save_config({"url": "http://192.168.1.1"})
        broker.store["openwrt_ssh_password"] = "senha"
        settings = Settings(homelab_enabled=False)
        status = mod.public_status(make_ow_services(settings))
        assert status["state"] == "DISABLED"

    async def test_ready_flow_via_adapter_path(self, ow_env):
        """§7: auth válida → READY; teste usa services.homelab.openwrt_status."""
        mod, broker, _ = ow_env
        mod.save_config({"url": "http://192.168.1.1", "username": "root"})
        broker.store["openwrt_ssh_password"] = "senha-correta"
        homelab = StubHomelab()
        result = await mod.test_connection(make_ow_services(Settings(), homelab))
        assert result["ok"] is True and result["state"] == "READY"
        assert result["authenticated"] is True
        assert result["version"] == "OpenWrt 23.05.3"
        assert homelab.calls == 1

        status = mod.public_status(make_ow_services(Settings()))
        assert status["state"] == "READY"
        assert status["authenticated"] is True

    async def test_missing_credential_short_circuits_before_adapter(self, ow_env):
        mod, _, _ = ow_env
        mod.save_config({"url": "http://192.168.1.1"})
        homelab = StubHomelab()
        result = await mod.test_connection(make_ow_services(Settings(), homelab))
        assert result["state"] == "UNCONFIGURED"
        assert result["error_code"] == "REMOTE_AUTH_MISSING"
        assert homelab.calls == 0  # nenhum comando remoto sai sem credencial

    async def test_auth_failed_mapping(self, ow_env):
        mod, broker, _ = ow_env
        mod.save_config({"url": "http://192.168.1.1"})
        broker.store["openwrt_ssh_password"] = "senha-recusada"
        homelab = StubHomelab(fail_with=SshAdapterError(
            "REMOTE_AUTH_FAILED", "Autenticação SSH rejeitada."))
        result = await mod.test_connection(make_ow_services(Settings(), homelab))
        assert result["ok"] is False
        assert result["state"] == "AUTH_FAILED"
        assert result["error_code"] == "REMOTE_AUTH_FAILED"

        status = mod.public_status(make_ow_services(Settings()))
        assert status["state"] == "AUTH_FAILED"  # nunca OFFLINE/READY

    async def test_offline_mapping(self, ow_env):
        mod, broker, _ = ow_env
        mod.save_config({"url": "http://10.255.255.1"})
        broker.store["openwrt_ssh_password"] = "senha"
        homelab = StubHomelab(fail_with=SshAdapterError(
            "REMOTE_EXECUTION_FAILED", "A conexão SSH falhou."))
        result = await mod.test_connection(make_ow_services(Settings(), homelab))
        assert result["state"] == "OFFLINE"

        status = mod.public_status(make_ow_services(Settings()))
        assert status["state"] == "OFFLINE"

    def test_apply_to_runtime_mirrors_attributes(self, ow_env):
        mod, broker, _ = ow_env
        mod.save_config({"url": "http://192.168.9.9", "username": "nyra"})
        broker.store["openwrt_ssh_password"] = "s3cret!"
        settings = Settings()
        summary = mod.apply_to_runtime(make_ow_services(settings))
        assert summary["applied"] is True
        assert settings.openwrt_url == "http://192.168.9.9"
        assert settings.openwrt_username == "nyra"
        assert settings.openwrt_password == "s3cret!"


# ================================================================ rotas HTTP


def build_route_client(services):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import router

    app = FastAPI()
    app.include_router(router)
    app.state.services = services
    return TestClient(app)


class TestOpenWrtRoutes:
    def _patched(self, tmp_path, monkeypatch, broker=None):
        from app.integrations.openwrt import config as ow_mod

        monkeypatch.setattr(ow_mod, "CONFIG_PATH", tmp_path / "openwrt-config.json")
        monkeypatch.setattr(ow_mod, "load_runtime_settings", lambda: {})
        broker = broker or StubBroker()
        monkeypatch.setattr(ow_mod, "_broker", lambda: broker)
        return ow_mod, broker

    def test_config_get_masks_secret(self, tmp_path, monkeypatch):
        _, broker = self._patched(tmp_path, monkeypatch)
        broker.store["openwrt_ssh_password"] = "NYRA_SECRET_LEAK_TEST_route"
        client = build_route_client(make_ow_services(Settings()))
        response = client.get("/api/openwrt/config")
        assert response.status_code == 200
        payload = response.json()
        assert payload["id"] == "openwrt"
        assert payload["auth_configured"] is True
        assert "NYRA_SECRET_LEAK_TEST_route" not in response.text

    def test_config_put_saves_credentials_and_runtime(self, tmp_path, monkeypatch):
        _, broker = self._patched(tmp_path, monkeypatch)
        settings = Settings()
        homelab = StubHomelab()
        client = build_route_client(make_ow_services(settings, homelab))
        response = client.put("/api/openwrt/config", json={
            "url": "http://192.168.1.1", "username": "root",
            "password": "senha-da-ui",
        })
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["credentials"]["password_configured"] is True
        assert body["runtime_applied"]["auth_configured"] is True
        assert broker.store["openwrt_ssh_password"] == "senha-da-ui"
        assert "senha-da-ui" not in response.text

    def test_test_route_executes_real_adapter_path(self, tmp_path, monkeypatch):
        ow_mod, broker = self._patched(tmp_path, monkeypatch)
        ow_mod.save_config({"url": "http://192.168.1.1"})
        broker.store["openwrt_ssh_password"] = "senha"
        homelab = StubHomelab()
        client = build_route_client(make_ow_services(Settings(), homelab))
        response = client.post("/api/openwrt/test")
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "READY"
        assert homelab.calls == 1
