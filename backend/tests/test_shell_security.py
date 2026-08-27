from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.turn import current_turn_id
from app.events import EventBus
from app.tools.shell_executor import RawShellResult, ShellExecutor
from app.tools.shell_models import ShellErrorCode, ShellRiskLevel
from app.tools.shell_risk import ShellRiskClassifier
from app.tools.system_shell import SystemShellService


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ping 192.168.1.1", ShellRiskLevel.READ_ONLY),
        ("git status", ShellRiskLevel.READ_ONLY),
        ("Get-Process", ShellRiskLevel.READ_ONLY),
        ("Get-Service", ShellRiskLevel.READ_ONLY),
        ("mkdir diagnostics", ShellRiskLevel.LOW_RISK),
        ("Restart-Service spooler", ShellRiskLevel.ELEVATED),
        ("Start-Process powershell -Verb RunAs", ShellRiskLevel.ELEVATED),
        ("Remove-Item test.txt", ShellRiskLevel.DESTRUCTIVE),
        (r"Remove-Item -Recurse C:\temp", ShellRiskLevel.DESTRUCTIVE),
        ("git reset --hard", ShellRiskLevel.DESTRUCTIVE),
        ("git clean -fd", ShellRiskLevel.DESTRUCTIVE),
        ("format", ShellRiskLevel.CRITICAL),
        ("diskpart", ShellRiskLevel.CRITICAL),
    ],
)
def test_required_risk_classification(command: str, expected: ShellRiskLevel):
    assert ShellRiskClassifier().classify(command).level == expected


def test_pipeline_redirection_aliases_and_unknown_executable_are_not_naively_safe():
    classifier = ShellRiskClassifier()
    assert classifier.classify("Get-ChildItem | Remove-Item").level == ShellRiskLevel.DESTRUCTIVE
    assert classifier.classify("Get-Process > processes.txt").level == ShellRiskLevel.LOW_RISK
    assert classifier.classify("rm -Recurse .\\cache").level == ShellRiskLevel.DESTRUCTIVE
    assert classifier.classify("Invoke-Expression $payload").level == ShellRiskLevel.ELEVATED
    assert classifier.classify("some-unknown-tool --do-something").level == ShellRiskLevel.ELEVATED


@pytest.mark.parametrize(
    ("command", "shell"),
    [
        ('Write-Output "$(& \'cmd.exe\' /c \'whoami\')"', "powershell"),
        ("Write-Output ([System.IO.File]::WriteAllText('x','y'))", "powershell"),
        ("Write-Output (Start-Process calc.exe)", "powershell"),
        ("Write-Output ([System.IO.FileInfo]'x').'Delete'()", "powershell"),
        ('echo "$(/usr/bin/id)"', "bash"),
        ("echo `id`", "bash"),
        ("cat <(id)", "bash"),
    ],
)
def test_nested_shell_execution_is_never_read_only(command: str, shell: str):
    assert ShellRiskClassifier().classify(command, shell).level in {
        ShellRiskLevel.ELEVATED,
        ShellRiskLevel.DESTRUCTIVE,
        ShellRiskLevel.CRITICAL,
    }


class RecordingExecutor(ShellExecutor):
    def __init__(self) -> None:
        self.commands: list[str] = []

    def resolve_executable(self, shell: str) -> str | None:
        return "fake.exe"

    async def execute(self, command: str, shell: str, timeout_seconds: int, working_directory: Path) -> RawShellResult:
        self.commands.append(command)
        return RawShellResult("fake.exe", 0, b"executed", b"", 4.2)


async def create_service(tmp_path: Path) -> tuple[SystemShellService, RecordingExecutor]:
    executor = RecordingExecutor()
    settings = Settings.from_sources(
        database_path=tmp_path / "approval.db",
        shell_enabled=True,
        shell_default="powershell",
        shell_timeout_seconds=30,
        shell_max_timeout_seconds=300,
        shell_max_output_chars=10_000,
        shell_max_calls_per_turn=10,
        shell_confirm_destructive=True,
        shell_approval_ttl_seconds=300,
        shell_default_working_directory=tmp_path,
    )
    shell = SystemShellService(settings, EventBus(), executor=executor)
    await shell.initialize()
    return shell, executor


@pytest.mark.asyncio
async def test_read_only_executes_without_approval(tmp_path: Path):
    shell, executor = await create_service(tmp_path)
    token = current_turn_id.set("turn_shell_events")
    try:
        result = await shell.execute("Get-Process")
    finally:
        current_turn_id.reset(token)
    assert result["success"] is True
    assert executor.commands == ["Get-Process"]
    execution_events = [
        event for event in shell.event_bus.history()
        if event.type.value in {"SHELL_EXECUTION_STARTED", "SHELL_EXECUTION_FINISHED"}
    ]
    assert len(execution_events) == 2
    assert all(event.payload["turn_id"] == "turn_shell_events" for event in execution_events)


@pytest.mark.asyncio
async def test_low_risk_executes_without_approval_and_elevated_does_not(tmp_path: Path):
    shell, executor = await create_service(tmp_path)
    low = await shell.execute("mkdir diagnostics")
    elevated = await shell.execute("Restart-Service spooler")
    assert low["success"] is True and low["risk_level"] == "LOW_RISK"
    assert elevated["error_code"] == ShellErrorCode.APPROVAL_REQUIRED.value
    assert executor.commands == ["mkdir diagnostics"]


@pytest.mark.asyncio
async def test_destructive_requires_then_valid_approval_releases_exact_command_once(tmp_path: Path):
    shell, executor = await create_service(tmp_path)
    pending = await shell.execute("Remove-Item test.txt")
    assert pending["error_code"] == ShellErrorCode.APPROVAL_REQUIRED.value
    assert executor.commands == []

    approval_id = pending["approval_id"]
    assert await shell.decide_approval(approval_id, True)
    executed = await shell.execute("Remove-Item test.txt", approval_id=approval_id)
    assert executed["success"] is True
    assert executed["approval_granted"] is True
    assert executor.commands == ["Remove-Item test.txt"]

    replay = await shell.execute("Remove-Item test.txt", approval_id=approval_id)
    assert replay["error_code"] == ShellErrorCode.COMMAND_REJECTED.value
    assert executor.commands == ["Remove-Item test.txt"]


@pytest.mark.asyncio
async def test_invalid_or_other_command_approval_is_rejected(tmp_path: Path):
    shell, executor = await create_service(tmp_path)
    invalid = await shell.execute("Remove-Item a.txt", approval_id="x" * 24)
    assert invalid["error_code"] == ShellErrorCode.COMMAND_REJECTED.value

    pending = await shell.execute("Remove-Item a.txt")
    approval_id = pending["approval_id"]
    await shell.decide_approval(approval_id, True)
    other = await shell.execute("Remove-Item b.txt", approval_id=approval_id)
    assert other["error_code"] == ShellErrorCode.COMMAND_REJECTED.value
    assert executor.commands == []


@pytest.mark.asyncio
async def test_only_strict_operator_reply_grants_pending_conversation_approval(tmp_path: Path):
    shell, _ = await create_service(tmp_path)
    pending = await shell.execute("git reset --hard")
    assert await shell.resolve_user_approval("talvez sim depois") is None
    granted = await shell.resolve_user_approval("sim, autorizo")
    assert granted is not None and granted.approval_id == pending["approval_id"]
