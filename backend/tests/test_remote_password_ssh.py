"""Transporte SSH programático por senha para OpenWrt (openwrt-fix.md).

Cobre: roteamento quando há senha no Broker, preservação de key auth,
estados coerentes, TOFU persistido e garantia de zero vazamento de segredo
em resultados/eventos/histórico/auditoria.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.events import EventBus
from app.operator.credentials import CredentialBroker
from app.tools.password_ssh_executor import (
    AsyncSSHPasswordExecutor,
    HostKeyTofuStore,
)
from app.tools.remote_executor import RawRemoteResult
from app.tools.remote_models import RemoteShellErrorCode
from app.tools.remote_shell import RemoteShellService
from app.tools.shell_approval import ShellApprovalGate

SECRET = "s3cret-NYRA-openwrt-PW"


class FakePasswordExecutor(AsyncSSHPasswordExecutor):
    def __init__(self, result: RawRemoteResult | None = None, store_path: Path | None = None) -> None:
        super().__init__(store_path=store_path)
        self.calls: list[dict] = []
        self.result = result or RawRemoteResult("asyncssh(password)", 0, b"ok\n", b"", 5.0)

    async def execute(self, **kwargs) -> RawRemoteResult:
        self.calls.append(kwargs)
        return self.result


class FakeSSHExecutor:
    """Stub do transporte ssh.exe com a mesma superfície usada pelo serviço."""

    def __init__(self, result: RawRemoteResult | None = None) -> None:
        self.result = result or RawRemoteResult("ssh.exe", 0, b"ok\n", b"", 12.5)
        self.calls: list[tuple[str, str]] = []

    def resolve_executable(self) -> str | None:
        return "ssh.exe"

    async def execute(self, host, command: str, **kwargs) -> RawRemoteResult:
        self.calls.append((host.id, command))
        return self.result


def registry_file(tmp_path: Path, private_key: Path | None = None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("", encoding="utf-8")
    keyed = {
        "enabled": True, "port": 22, "username": "nyra", "platform": "openwrt",
        "capabilities": ["diagnostics", "network"],
        "known_hosts_path": str(known_hosts), "use_ssh_agent": False,
    }
    if private_key is not None:
        keyed["private_key_path"] = str(private_key)
    path = tmp_path / "hosts.json"
    path.write_text(json.dumps({"hosts": [
        {
            "id": "gateway", "address": "192.168.1.1", "aliases": ["openwrt", "gateway"],
            "remote_shell": {
                "enabled": True, "port": 22, "username": "nyra", "platform": "openwrt",
                "capabilities": ["diagnostics", "network"],
                "known_hosts_path": str(known_hosts), "use_ssh_agent": True,
            },
        },
        {"id": "keyed", "address": "192.168.1.1", "aliases": ["comchave"],
         "remote_shell": keyed},
        {
            "id": "noauth", "address": "192.168.1.9", "aliases": ["semcredencial"],
            "remote_shell": {
                "enabled": True, "port": 22, "username": "nyra", "platform": "linux",
                "capabilities": ["diagnostics"],
                "known_hosts_path": str(known_hosts), "use_ssh_agent": False,
            },
        },
    ]}, ensure_ascii=False), encoding="utf-8")
    return path


def configure_broker(monkeypatch, password: str) -> None:
    monkeypatch.setattr(
        "app.integrations.openwrt.config.load_config",
        lambda settings: {"url": "192.168.1.1", "username": "root",
                          "last_test": {}, "updated_at": None},
    )
    monkeypatch.setattr(
        "app.integrations.openwrt.config.resolve_password",
        lambda settings: password,
    )


async def service(tmp_path: Path, *, ssh_executor=None, password_executor=None) -> RemoteShellService:
    path = registry_file(tmp_path)
    settings = Settings.from_sources(
        database_path=tmp_path / "remote.db",
        trusted_hosts_path=path,
        remote_shell_enabled=True,
    )
    remote = RemoteShellService(
        settings, EventBus(), ShellApprovalGate(),
        executor=ssh_executor if ssh_executor is not None else FakeSSHExecutor(),
        password_executor=password_executor,
    )
    await remote.initialize()
    return remote


@pytest.mark.asyncio
async def test_openwrt_password_transport_authenticates_without_ssh_exe(monkeypatch, tmp_path: Path):
    configure_broker(monkeypatch, SECRET)
    ssh = FakeSSHExecutor()
    passwords = FakePasswordExecutor(store_path=tmp_path / "keys")
    remote = await service(tmp_path, ssh_executor=ssh, password_executor=passwords)
    result = await remote.execute("openwrt", "ubus call system info")
    assert result["success"] is True and result["stdout"] == "ok\n"
    # ssh.exe NUNCA é usado para password auth desta integração.
    assert ssh.calls == []
    assert len(passwords.calls) == 1
    call = passwords.calls[0]
    assert call["address"] == "192.168.1.1" and call["port"] == 22
    # usuário vem da config da integração (root), não do registry (nyra)
    assert call["username"] == "root"
    assert call["password"] == SECRET
    assert call["command"] == "ubus call system info"
    # timeouts explícitos e curtos
    assert 0 < call["connect_timeout_seconds"] <= 10
    assert 0 < call["command_timeout_seconds"] <= 60


@pytest.mark.asyncio
async def test_password_transport_rejects_missing_known_hosts_file(monkeypatch, tmp_path: Path):
    configure_broker(monkeypatch, SECRET)
    path = registry_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["hosts"][0]["remote_shell"]["known_hosts_path"] = str(tmp_path / "inexistente")
    path.write_text(json.dumps(data), encoding="utf-8")
    settings = Settings.from_sources(database_path=tmp_path / "db.db", trusted_hosts_path=path)
    remote = RemoteShellService(settings, EventBus(), ShellApprovalGate(),
                                executor=FakeSSHExecutor(),
                                password_executor=FakePasswordExecutor(store_path=tmp_path / "keys"))
    await remote.initialize()
    result = await remote.execute("openwrt", "echo NYRA_SSH_OK")
    assert result["success"] is False
    assert result["error_code"] == RemoteShellErrorCode.SSH_KNOWN_HOSTS_MISSING.value


@pytest.mark.asyncio
async def test_non_openwrt_host_keeps_existing_behavior(monkeypatch, tmp_path: Path):
    configure_broker(monkeypatch, SECRET)
    ssh = FakeSSHExecutor()
    passwords = FakePasswordExecutor(store_path=tmp_path / "keys")
    remote = await service(tmp_path, ssh_executor=ssh, password_executor=passwords)
    result = await remote.execute("semcredencial", "uptime")
    assert result["error_code"] == RemoteShellErrorCode.SSH_CREDENTIALS_MISSING.value
    assert passwords.calls == []


@pytest.mark.asyncio
async def test_key_auth_preserved_then_password_retry_on_auth_failure(monkeypatch, tmp_path: Path):
    configure_broker(monkeypatch, SECRET)
    private_key = tmp_path / "id_ed25519"
    private_key.write_text("FAKE KEY", encoding="utf-8")
    path = registry_file(tmp_path, private_key=private_key)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["hosts"][0]["remote_shell"] = data["hosts"][1]["remote_shell"]
    path.write_text(json.dumps(data), encoding="utf-8")
    settings = Settings.from_sources(database_path=tmp_path / "db2.db", trusted_hosts_path=path)
    ssh = FakeSSHExecutor(RawRemoteResult("ssh.exe", 255, b"", b"Permission denied (publickey,password).", 20))
    passwords = FakePasswordExecutor(store_path=tmp_path / "keys")
    remote = RemoteShellService(settings, EventBus(), ShellApprovalGate(),
                                executor=ssh, password_executor=passwords)
    await remote.initialize()
    result = await remote.execute("comchave", "uptime")
    # chave tentada primeiro e recusada → retry real via senha → sucesso
    assert [call[0] for call in ssh.calls] == ["keyed"]
    assert len(passwords.calls) == 1
    assert result["success"] is True


@pytest.mark.asyncio
async def test_no_password_transport_without_broker_credential(monkeypatch, tmp_path: Path):
    configure_broker(monkeypatch, "")
    passwords = FakePasswordExecutor(store_path=tmp_path / "keys")
    remote = await service(tmp_path, password_executor=passwords)
    result = await remote.execute("openwrt", "uptime")
    assert passwords.calls == []
    # sem senha, fluxo legado segue (agent habilitado → ssh.exe)
    assert result["success"] is True


def test_tofu_store_first_use_trust_then_mismatch_blocks(tmp_path: Path):
    store = HostKeyTofuStore(tmp_path / "gateway.json")
    assert store.matches("192.168.1.1", 22, "ssh-ed25519", "SHA256:abc") is None
    store.record("192.168.1.1", 22, "ssh-ed25519", "SHA256:abc", "ssh-ed25519 AAAAFAKE")
    assert store.matches("192.168.1.1", 22, "ssh-ed25519", "SHA256:abc") is True
    assert store.matches("192.168.1.1", 22, "ssh-ed25519", "SHA256:hacked") is False
    assert store.matches("192.168.1.1", 22, "rsa-sha2-512", "SHA256:other") is False
    persisted = HostKeyTofuStore(tmp_path / "gateway.json")
    assert persisted.matches("192.168.1.1", 22, "ssh-ed25519", "SHA256:abc") is True
    document = json.loads((tmp_path / "gateway.json").read_text(encoding="utf-8"))
    entry = document["192.168.1.1:22"]
    assert entry["first_trusted_at"] > 0 and entry["keys"]["ssh-ed25519"]["fingerprint_sha256"] == "SHA256:abc"


def test_tofu_store_exports_openssh_known_hosts_for_asyncssh(tmp_path: Path):
    store = HostKeyTofuStore(tmp_path / "gateway.json")
    store.record("192.168.1.1", 22, "ssh-ed25519", "SHA256:abc", "ssh-ed25519 AAAAFAKE")
    destination = tmp_path / "gateway.known_hosts"
    store.export_openssh_known_hosts("192.168.1.1", 22, destination)
    content = destination.read_text(encoding="ascii").strip()
    assert content == "192.168.1.1,192.168.1.1:22 ssh-ed25519 AAAAFAKE"
    # regeneração idempotente e sem chaves desconhecidas
    store.export_openssh_known_hosts("192.168.1.1", 22, destination)
    assert len(destination.read_text(encoding="ascii").strip().splitlines()) == 1


@pytest.mark.asyncio
async def test_secret_never_leaks_into_results_events_or_history(monkeypatch, tmp_path: Path):
    configure_broker(monkeypatch, SECRET)
    passwords = FakePasswordExecutor(store_path=tmp_path / "keys")
    remote = await service(tmp_path, password_executor=passwords)
    token_result = await remote.execute("openwrt", "echo NYRA_SSH_OK")
    assert token_result["success"] is True

    payloads = [
        json.dumps(token_result, ensure_ascii=False, default=str),
        json.dumps([event.payload for event in remote.event_bus.history()],
                   ensure_ascii=False, default=str),
        json.dumps(await remote.history.recent(5), ensure_ascii=False, default=str),
        json.dumps(remote.status(), ensure_ascii=False, default=str),
        CredentialBroker().list_credentials().__str__(),
    ]
    for payload in payloads:
        assert SECRET not in payload
