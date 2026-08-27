from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.config import Settings
from app.events import EventBus, EventType
from app.tools.shell_executor import RawShellResult, ShellExecutor, decode_output
from app.tools.shell_models import ShellErrorCode
from app.tools.system_shell import SystemShellService


class FakeExecutor(ShellExecutor):
    def __init__(self, stdout: bytes = b"fake real output", exit_code: int = 0) -> None:
        self.stdout = stdout
        self.exit_code = exit_code
        self.calls: list[tuple[str, str, int, Path]] = []

    def resolve_executable(self, shell: str) -> str | None:
        return f"{shell}.exe"

    async def execute(self, command: str, shell: str, timeout_seconds: int, working_directory: Path) -> RawShellResult:
        self.calls.append((command, shell, timeout_seconds, working_directory))
        return RawShellResult(
            executable=f"{shell}.exe",
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr=b"",
            duration_ms=12.5,
        )


def shell_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        database_path=tmp_path / "nyra-shell.db",
        shell_enabled=True,
        shell_default="powershell",
        shell_timeout_seconds=3,
        shell_max_timeout_seconds=5,
        shell_max_output_chars=1_000,
        shell_max_calls_per_turn=10,
        shell_confirm_destructive=True,
        shell_approval_ttl_seconds=300,
        shell_default_working_directory=tmp_path,
    )
    values.update(overrides)
    return Settings.from_sources(**values)


async def service(tmp_path: Path, executor: ShellExecutor | None = None, **settings) -> SystemShellService:
    value = SystemShellService(shell_settings(tmp_path, **settings), EventBus(), executor=executor)
    await value.initialize()
    return value


@pytest.mark.skipif(os.name != "nt" or ShellExecutor().resolve_executable("powershell") is None, reason="PowerShell is required")
@pytest.mark.asyncio
async def test_powershell_captures_unicode_stdout_and_zero_exit(tmp_path: Path):
    shell = await service(tmp_path)
    result = await shell.execute("Write-Output 'Olá, NYRA ✓'", reason="teste unicode")
    assert result["success"] is True
    assert result["exit_code"] == 0
    assert "Olá, NYRA" in result["stdout"]
    assert result["shell"] == "powershell"
    assert result["risk_level"] == "READ_ONLY"


@pytest.mark.skipif(os.name != "nt" or ShellExecutor().resolve_executable("powershell") is None, reason="PowerShell is required")
@pytest.mark.asyncio
async def test_powershell_decodes_legacy_windows_native_output(tmp_path: Path):
    shell = await service(tmp_path)
    result = await shell.execute("ipconfig")
    assert result["success"] is True
    assert "Configuração de IP do Windows" in result["stdout"]
    assert "�" not in result["stdout"]


@pytest.mark.skipif(os.name != "nt" or ShellExecutor().resolve_executable("cmd") is None, reason="CMD is required")
@pytest.mark.asyncio
async def test_cmd_captures_stdout(tmp_path: Path):
    shell = await service(tmp_path)
    result = await shell.execute("echo NYRA-CMD", shell="cmd")
    assert result["success"] is True
    assert result["exit_code"] == 0
    assert "NYRA-CMD" in result["stdout"]


@pytest.mark.skipif(os.name != "nt" or ShellExecutor().resolve_executable("powershell") is None, reason="PowerShell is required")
@pytest.mark.asyncio
async def test_nonzero_exit_and_stderr_are_structured(tmp_path: Path):
    shell = await service(tmp_path)
    result = await shell.execute("Write-Error 'falha esperada'; exit 7")
    assert result["success"] is False
    assert result["exit_code"] == 7
    assert result["error_code"] == ShellErrorCode.EXECUTION_FAILED.value
    assert "falha esperada" in result["stderr"]


@pytest.mark.skipif(os.name != "nt" or ShellExecutor().resolve_executable("powershell") is None, reason="PowerShell is required")
@pytest.mark.asyncio
async def test_powershell_nonterminating_error_is_not_reported_as_success(tmp_path: Path):
    shell = await service(tmp_path)
    result = await shell.execute("Write-Error 'falha não terminante'")
    assert result["success"] is False
    assert result["exit_code"] == 0
    assert result["error_code"] == ShellErrorCode.EXECUTION_FAILED.value
    assert "falha não terminante" in result["stderr"]


@pytest.mark.skipif(os.name != "nt" or ShellExecutor().resolve_executable("powershell") is None, reason="PowerShell is required")
@pytest.mark.asyncio
async def test_timeout_terminates_finite_tool_call(tmp_path: Path):
    shell = await service(tmp_path)
    result = await shell.execute("Start-Sleep -Seconds 3", timeout_seconds=1)
    assert result["success"] is False
    assert result["timed_out"] is True
    assert result["error_code"] == ShellErrorCode.EXECUTION_TIMEOUT.value


@pytest.mark.asyncio
async def test_empty_command_invalid_cwd_and_feature_flag_are_structured(tmp_path: Path):
    shell = await service(tmp_path, executor=FakeExecutor())
    empty = await shell.execute("   ")
    missing = await shell.execute("Get-Date", working_directory=str(tmp_path / "missing"))
    invalid_timeout = await shell.execute("Get-Date", timeout_seconds=0)
    disabled = await service(tmp_path / "disabled", executor=FakeExecutor(), shell_enabled=False)
    off = await disabled.execute("Get-Date")
    assert empty["error_code"] == ShellErrorCode.INVALID_COMMAND.value
    assert missing["error_code"] == ShellErrorCode.INVALID_WORKING_DIRECTORY.value
    assert invalid_timeout["error_code"] == ShellErrorCode.INVALID_COMMAND.value
    assert off["error_code"] == ShellErrorCode.SHELL_DISABLED.value


@pytest.mark.skipif(os.name != "nt" or ShellExecutor().resolve_executable("powershell") is None, reason="PowerShell is required")
@pytest.mark.asyncio
async def test_valid_cwd_and_output_truncation_preserve_head_and_tail(tmp_path: Path):
    shell = await service(tmp_path)
    location = await shell.execute("Get-Location", working_directory=str(tmp_path))
    command = "Write-Output ('HEAD' + ('x' * 4000) + 'TAIL')"
    pending = await shell.execute(command)
    assert pending["error_code"] == ShellErrorCode.APPROVAL_REQUIRED.value
    shell.approvals.grant(pending["approval_id"], "test")
    output = await shell.execute(command, approval_id=pending["approval_id"])
    assert str(tmp_path).casefold() in location["stdout"].casefold()
    assert output["success"] is True
    assert output["approval_granted"] is True
    assert output["stdout_truncated"] is True
    assert "HEAD" in output["stdout"] and "TAIL" in output["stdout"]
    assert "NYRA OUTPUT TRUNCATED" in output["stdout"]
    assert len(output["stdout"]) <= 1_000


def test_alternative_windows_code_page_has_safe_fallback():
    assert decode_output("ação técnica".encode("cp850")) == "ação técnica"
    assert "�" not in decode_output(b"legacy:\xff")


@pytest.mark.asyncio
async def test_history_stores_limited_metadata_not_stdout(tmp_path: Path):
    fake = FakeExecutor(stdout=b"private output")
    shell = await service(tmp_path, executor=fake)
    result = await shell.execute("Get-Date", reason="clock diagnostic")
    recent = await shell.history.recent()
    assert recent[0].id == result["execution_id"]
    assert recent[0].command == "Get-Date"
    assert recent[0].reason == "clock diagnostic"
    assert "private output" not in recent[0].model_dump_json()


@pytest.mark.asyncio
async def test_findstr_exit_one_without_output_is_structured_no_match(tmp_path: Path):
    shell = await service(tmp_path, executor=FakeExecutor(stdout=b"", exit_code=1))
    result = await shell.execute("netstat -ano | findstr :5173", shell="cmd")
    assert result["success"] is True
    assert result["exit_code"] == 1
    assert result["error_code"] is None
    assert "não há listener" in result["message"]


@pytest.mark.asyncio
async def test_secrets_are_redacted_from_result_and_events(tmp_path: Path):
    bus = EventBus()
    fake = FakeExecutor(stdout=b"API_KEY=super-secret-value")
    shell = SystemShellService(shell_settings(tmp_path), bus, executor=fake)
    await shell.initialize()
    result = await shell.execute("Get-Date", reason="token=operator-secret")
    assert "super-secret-value" not in result["stdout"]
    assert "operator-secret" not in result["reason"]
    assert EventType.SHELL_EXECUTION_FINISHED in [event.type for event in bus.history()]
