from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.turn import current_turn_id
from app.agent.context import current_agent_run_id
from app.events import EventBus
from app.network_aliases import NetworkAliasRegistry
from app.tools.remote_executor import OpenSSHExecutor, RawRemoteResult
from app.tools.remote_models import RemoteShellErrorCode, RemoteShellExecuteInput
from app.tools.remote_shell import RemoteShellService
from app.tools.registry import create_tool_registry
from app.tools.shell_approval import ShellApprovalGate
from app.tools.shell_models import ShellRiskLevel
from app.tools.shell_risk import ShellRiskClassifier


class FakeSSHExecutor(OpenSSHExecutor):
    def __init__(self, result: RawRemoteResult | None = None) -> None:
        self.result = result or RawRemoteResult("ssh.exe", 0, b"ok\n", b"", 12.5)
        self.calls: list[tuple[str, str]] = []

    def resolve_executable(self) -> str | None:
        return "ssh.exe"

    async def execute(self, host, command: str, **kwargs) -> RawRemoteResult:
        self.calls.append((host.id, command))
        return self.result


def registry_file(tmp_path: Path) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("proxmox ssh-ed25519 fake-key\n", encoding="utf-8")
    path = tmp_path / "hosts.json"
    path.write_text(json.dumps({"hosts": [
        {
            "id": "proxmox", "address": "192.168.1.2", "aliases": ["proxmox", "servidor"],
            "remote_shell": {
                "enabled": True, "port": 22, "username": "nyra", "platform": "linux",
                "capabilities": ["diagnostics", "logs", "network", "service_management", "containers", "virtualization", "storage"],
                "known_hosts_path": str(known_hosts), "use_ssh_agent": True,
                "auto_remediation_actions": [], "managed_resources": {},
            },
        },
        {
            "id": "openwrt", "address": "192.168.1.1", "aliases": ["gateway", "roteador"],
            "remote_shell": {
                "enabled": True, "username": "nyra", "platform": "openwrt",
                "capabilities": ["diagnostics", "logs", "network", "service_management"],
                "known_hosts_path": str(known_hosts), "use_ssh_agent": True,
            },
        },
        {
            "id": "dc1", "address": "192.168.1.10", "aliases": ["domain controller"],
            "remote_shell": {"enabled": False, "platform": "windows", "capabilities": ["diagnostics"]},
        },
        {
            "id": "noauth", "address": "192.168.1.9", "aliases": ["sem credencial"],
            "remote_shell": {
                "enabled": True, "username": "nyra", "platform": "linux", "capabilities": ["diagnostics"],
                "known_hosts_path": str(known_hosts), "use_ssh_agent": False,
            },
        },
    ]}, ensure_ascii=False), encoding="utf-8")
    return path, known_hosts


async def service(tmp_path: Path, executor: FakeSSHExecutor | None = None, **overrides) -> RemoteShellService:
    path, _ = registry_file(tmp_path)
    settings = Settings.from_sources(
        database_path=tmp_path / "remote.db",
        trusted_hosts_path=path,
        remote_shell_enabled=overrides.pop("remote_shell_enabled", True),
        ssh_max_output_chars=overrides.pop("ssh_max_output_chars", 50_000),
        **overrides,
    )
    remote = RemoteShellService(settings, EventBus(), ShellApprovalGate(), executor=executor or FakeSSHExecutor())
    await remote.initialize()
    return remote


@pytest.mark.asyncio
async def test_registered_host_and_alias_execute_real_structured_path(tmp_path: Path):
    executor = FakeSSHExecutor(RawRemoteResult("ssh.exe", 0, "saúde normal\n".encode(), b"", 8.0))
    remote = await service(tmp_path, executor)
    token = current_turn_id.set("turn_remote_events")
    try:
        result = await remote.execute("servidor", "uptime")
    finally:
        current_turn_id.reset(token)
    assert result["success"] is True
    assert result["host"] == "proxmox" and result["address"] == "192.168.1.2"
    assert result["stdout"] == "saúde normal\n"
    assert result["risk_level"] == "READ_ONLY"
    assert executor.calls == [("proxmox", "uptime")]
    history = await remote.history.recent(1)
    assert history[0].host == "proxmox" and history[0].success is True
    execution_events = [
        event for event in remote.event_bus.history()
        if event.type.value in {"REMOTE_SHELL_EXECUTION_STARTED", "REMOTE_SHELL_EXECUTION_FINISHED"}
    ]
    assert len(execution_events) == 2
    assert all(event.payload["turn_id"] == "turn_remote_events" for event in execution_events)


@pytest.mark.asyncio
async def test_remote_feature_flag_disables_execution_and_llm_schema(tmp_path: Path):
    remote = await service(tmp_path, remote_shell_enabled=False)
    result = await remote.execute("proxmox", "uptime")
    tools = create_tool_registry(None, remote)
    assert result["error_code"] == RemoteShellErrorCode.REMOTE_SHELL_DISABLED.value
    assert not any(item["function"]["name"] == "remote_shell" for item in tools.llm_tools())


@pytest.mark.asyncio
async def test_remote_shell_is_exposed_to_native_tool_router(tmp_path: Path):
    remote = await service(tmp_path)
    tools = create_tool_registry(None, remote)
    assert any(item["function"]["name"] == "remote_shell" for item in tools.llm_tools())
    result = await tools.execute("remote_shell", {"host": "proxmox", "command": "uptime"})
    assert result.ok is True and result.data["host"] == "proxmox"


def test_remote_input_rejects_direct_ip():
    with pytest.raises(ValidationError):
        RemoteShellExecuteInput(host="192.168.1.2", command="uptime")


@pytest.mark.asyncio
async def test_unknown_disabled_and_missing_credentials(tmp_path: Path):
    remote = await service(tmp_path)
    assert (await remote.execute("inventado", "uptime"))["error_code"] == RemoteShellErrorCode.UNKNOWN_TRUSTED_HOST.value
    assert (await remote.execute("dc1", "hostname"))["error_code"] == RemoteShellErrorCode.HOST_DISABLED.value
    assert (await remote.execute("noauth", "hostname"))["error_code"] == RemoteShellErrorCode.SSH_CREDENTIALS_MISSING.value
    nested = await remote.execute("proxmox", "ssh root@10.0.0.5 uptime")
    assert nested["error_code"] == RemoteShellErrorCode.COMMAND_REJECTED.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (RawRemoteResult("ssh", 255, b"", b"REMOTE HOST IDENTIFICATION HAS CHANGED!", 4), RemoteShellErrorCode.SSH_HOST_KEY_MISMATCH),
        (RawRemoteResult("ssh", 255, b"", b"connect to host 192.168.1.2 port 22: Connection timed out", 5000), RemoteShellErrorCode.SSH_CONNECTION_TIMEOUT),
        (RawRemoteResult("ssh", 255, b"", b"Permission denied (publickey).", 20), RemoteShellErrorCode.SSH_AUTHENTICATION_FAILED),
        (RawRemoteResult("ssh", None, b"", b"", 30000, timed_out=True), RemoteShellErrorCode.SSH_COMMAND_TIMEOUT),
    ],
)
async def test_structured_ssh_transport_failures(tmp_path: Path, raw: RawRemoteResult, expected: RemoteShellErrorCode):
    remote = await service(tmp_path, FakeSSHExecutor(raw))
    result = await remote.execute("proxmox", "uptime")
    assert result["success"] is False and result["error_code"] == expected.value


@pytest.mark.asyncio
async def test_stdout_stderr_nonzero_and_output_truncation(tmp_path: Path):
    remote = await service(tmp_path, FakeSSHExecutor(RawRemoteResult("ssh", 7, b"partial", b"failure", 9)))
    failed = await remote.execute("proxmox", "uptime")
    assert failed["exit_code"] == 7 and failed["stdout"] == "partial" and failed["stderr"] == "failure"
    assert failed["error_code"] == RemoteShellErrorCode.SSH_COMMAND_FAILED.value

    remote = await service(tmp_path / "truncated", FakeSSHExecutor(RawRemoteResult("ssh", 0, b"x" * 3000, b"", 3)), ssh_max_output_chars=1000)
    truncated = await remote.execute("proxmox", "uptime")
    assert truncated["success"] is True and truncated["stdout_truncated"] is True
    assert "OUTPUT TRUNCATED" in truncated["stdout"] and len(truncated["stdout"]) <= 1000


@pytest.mark.asyncio
async def test_remote_working_directory_is_shell_quoted(tmp_path: Path):
    executor = FakeSSHExecutor()
    remote = await service(tmp_path, executor)
    result = await remote.execute("proxmox", "pwd", working_directory="/srv/UTAMO site")
    assert result["success"] is True
    assert executor.calls[0][1] == "cd '/srv/UTAMO site' && pwd"


@pytest.mark.asyncio
async def test_remote_destructive_approval_is_exact_and_single_use(tmp_path: Path):
    executor = FakeSSHExecutor()
    remote = await service(tmp_path, executor)
    pending = await remote.execute("proxmox", "systemctl restart nginx")
    assert pending["error_code"] == RemoteShellErrorCode.APPROVAL_REQUIRED.value
    approval_id = pending["approval_id"]
    assert remote.approvals.grant(approval_id) is not None
    mismatch = await remote.execute("proxmox", "systemctl restart pveproxy", approval_id=approval_id)
    assert mismatch["error_code"] == RemoteShellErrorCode.COMMAND_REJECTED.value

    # A mismatch does not consume the approval; the exact action can use it once.
    executed = await remote.execute("proxmox", "systemctl restart nginx", approval_id=approval_id)
    assert executed["success"] is True and executed["approval_granted"] is True
    replay = await remote.execute("proxmox", "systemctl restart nginx", approval_id=approval_id)
    assert replay["error_code"] == RemoteShellErrorCode.COMMAND_REJECTED.value


@pytest.mark.asyncio
async def test_remote_approval_is_bound_to_agent_run_id(tmp_path: Path):
    remote = await service(tmp_path)
    token = current_agent_run_id.set("run_original")
    try:
        pending = await remote.execute("proxmox", "systemctl restart nginx")
    finally:
        current_agent_run_id.reset(token)
    approval_id = pending["approval_id"]
    assert remote.approvals.grant(approval_id) is not None

    token = current_agent_run_id.set("run_other")
    try:
        rejected = await remote.execute("proxmox", "systemctl restart nginx", approval_id=approval_id)
    finally:
        current_agent_run_id.reset(token)
    assert rejected["error_code"] == RemoteShellErrorCode.COMMAND_REJECTED.value

    token = current_agent_run_id.set("run_original")
    try:
        accepted = await remote.execute("proxmox", "systemctl restart nginx", approval_id=approval_id)
    finally:
        current_agent_run_id.reset(token)
    assert accepted["success"] is True


@pytest.mark.asyncio
async def test_host_capability_is_enforced(tmp_path: Path):
    path, known_hosts = registry_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["hosts"][0]["remote_shell"]["capabilities"] = ["diagnostics"]
    path.write_text(json.dumps(data), encoding="utf-8")
    settings = Settings.from_sources(database_path=tmp_path / "cap.db", trusted_hosts_path=path)
    remote = RemoteShellService(settings, EventBus(), ShellApprovalGate(), executor=FakeSSHExecutor())
    await remote.initialize()
    result = await remote.execute("proxmox", "qm list")
    assert result["error_code"] == RemoteShellErrorCode.CAPABILITY_DENIED.value


@pytest.mark.asyncio
async def test_auto_remediation_requires_normalized_global_host_and_resource_allowlists(tmp_path: Path):
    path, _ = registry_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    remote_config = data["hosts"][0]["remote_shell"]
    remote_config["auto_remediation_actions"] = ["restart_known_service"]
    remote_config["managed_resources"] = {"services": ["nginx"]}
    path.write_text(json.dumps(data), encoding="utf-8")
    settings_value = Settings.from_sources(
        database_path=tmp_path / "auto.db", trusted_hosts_path=path,
        agent_auto_remediation=True,
        agent_auto_remediation_actions="restart_known_service",
    )
    executor = FakeSSHExecutor()
    remote = RemoteShellService(settings_value, EventBus(), ShellApprovalGate(), executor=executor)
    await remote.initialize()
    allowed = await remote.execute("proxmox", "systemctl restart nginx")
    unknown_resource = await remote.execute("proxmox", "systemctl restart pveproxy")
    injected_suffix = await remote.execute(
        "proxmox", "systemctl restart nginx; /usr/sbin/useradd intruder"
    )
    assert allowed["success"] is True and allowed["normalized_action"] == "restart_known_service"
    assert unknown_resource["error_code"] == RemoteShellErrorCode.APPROVAL_REQUIRED.value
    assert injected_suffix["error_code"] == RemoteShellErrorCode.APPROVAL_REQUIRED.value
    assert len(executor.calls) == 1


@pytest.mark.parametrize(
    ("command", "risk"),
    [
        ("uptime", ShellRiskLevel.READ_ONLY), ("uname -a", ShellRiskLevel.READ_ONLY),
        ("ip addr", ShellRiskLevel.READ_ONLY), ("systemctl status nginx", ShellRiskLevel.READ_ONLY),
        ("journalctl -n 20", ShellRiskLevel.READ_ONLY), ("qm list", ShellRiskLevel.READ_ONLY),
        ("systemctl --failed", ShellRiskLevel.READ_ONLY),
        ("pvesm status", ShellRiskLevel.READ_ONLY), ("wifi status", ShellRiskLevel.READ_ONLY),
        ("systemctl restart nginx", ShellRiskLevel.ELEVATED), ("wifi reload", ShellRiskLevel.ELEVATED),
        ("rm -rf /tmp/data", ShellRiskLevel.DESTRUCTIVE), ("qm destroy 100", ShellRiskLevel.DESTRUCTIVE),
        ("sudo rm -rf /tmp/data", ShellRiskLevel.DESTRUCTIVE),
        ("zfs destroy tank/data", ShellRiskLevel.DESTRUCTIVE), ("mkfs.ext4 /dev/sdb", ShellRiskLevel.CRITICAL),
    ],
)
def test_remote_risk_classifier(command: str, risk: ShellRiskLevel):
    assert ShellRiskClassifier().classify(command, "bash").level == risk


@pytest.mark.asyncio
async def test_openssh_executor_enforces_strict_host_key(monkeypatch, tmp_path: Path):
    path, known_hosts = registry_file(tmp_path)
    host = NetworkAliasRegistry(path).resolve("proxmox")
    captured: list[str] = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    async def fake_subprocess(*args, **kwargs):
        captured.extend(str(item) for item in args)
        return Process()

    monkeypatch.setattr("app.tools.remote_executor.asyncio.create_subprocess_exec", fake_subprocess)
    executor = OpenSSHExecutor()
    monkeypatch.setattr(executor, "resolve_executable", lambda: "ssh")
    result = await executor.execute(
        host, "uptime", connect_timeout_seconds=5, command_timeout_seconds=30,
        known_hosts_path=known_hosts, private_key_path=None,
    )
    joined = " ".join(captured)
    assert result.exit_code == 0
    assert "StrictHostKeyChecking=yes" in joined and "StrictHostKeyChecking=no" not in joined
    assert "BatchMode=yes" in joined and "192.168.1.2" in joined
