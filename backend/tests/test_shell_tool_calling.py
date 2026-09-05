from __future__ import annotations

from pathlib import Path
import json

import pytest

from app.core.config import Settings
from app.events import EventBus
from app.llm.base import LLMMessage, LLMProvider, LLMResponse, LLMToolCall, LLMToolFunction
from app.network_aliases import NetworkAliasRegistry
from app.tools import create_tool_registry
from app.tools.agent import ToolAgentLoop
from app.tools.shell_executor import RawShellResult, ShellExecutor
from app.tools.system_shell import SystemShellService


class PingExecutor(ShellExecutor):
    def __init__(self) -> None:
        self.commands: list[str] = []

    def resolve_executable(self, shell: str) -> str | None:
        return "powershell.exe"

    async def execute(self, command: str, shell: str, timeout_seconds: int, working_directory: Path) -> RawShellResult:
        self.commands.append(command)
        return RawShellResult("powershell.exe", 0, b"Reply from 192.168.1.2: bytes=32 time=1ms TTL=64", b"", 9.0)


class ToolCallingLLM(LLMProvider):
    def __init__(self) -> None:
        self.round = 0

    @property
    def name(self) -> str:
        return "scripted"

    async def health(self) -> bool:
        return True

    async def chat(self, messages: list[LLMMessage]) -> str:
        return "unused"

    async def complete(self, messages: list[LLMMessage], tools: list[dict] | None = None) -> LLMResponse:
        self.round += 1
        if self.round == 1:
            assert tools and any(item["function"]["name"] == "system_shell" for item in tools)
            return LLMResponse(tool_calls=[LLMToolCall(function=LLMToolFunction(
                name="system_shell",
                arguments={"command": "ping 192.168.1.2 -n 1", "reason": "verificar o Proxmox"},
            ))])
        tool_message = messages[-1]
        assert tool_message.role == "tool" and tool_message.tool_name == "system_shell"
        assert "Reply from 192.168.1.2" in tool_message.content
        return LLMResponse(content="O Proxmox respondeu em 1 ms usando o ping real.")


class HallucinatedPermissionLLM(LLMProvider):
    def __init__(self) -> None:
        self.round = 0

    @property
    def name(self) -> str:
        return "grounding-test"

    async def health(self) -> bool:
        return True

    async def chat(self, messages: list[LLMMessage]) -> str:
        return "unused"

    async def complete(self, messages: list[LLMMessage], tools: list[dict] | None = None) -> LLMResponse:
        self.round += 1
        if self.round == 1:
            return LLMResponse(tool_calls=[LLMToolCall(function=LLMToolFunction(
                name="system_shell", arguments={"command": "Get-NetAdapter"},
            ))])
        if self.round == 2:
            return LLMResponse(content="Não consegui listar: parece acesso negado e falta de permissão.")
        assert tools is None
        assert "GROUNDING CORRECTION REQUIRED" in messages[-1].content
        return LLMResponse(content="A interface Ethernet está ativa, conforme o stdout real.")


class FallbackExecutor(ShellExecutor):
    async def execute(self, command: str, shell: str, timeout_seconds: int, working_directory: Path) -> RawShellResult:
        if command == "Get-NetAdapter":
            return RawShellResult(
                "powershell.exe", 0, b"",
                b"Get-NetAdapter : Acesso negado\r\nCategoryInfo : PermissionDenied\r\nFullyQualifiedErrorId : HRESULT",
                3.0,
            )
        return RawShellResult("powershell.exe", 0, b"Ethernet IPv4 192.168.1.108", b"", 3.0)

    def resolve_executable(self, shell: str) -> str | None:
        return "powershell.exe"


class RetryLLM(LLMProvider):
    def __init__(self) -> None:
        self.round = 0

    @property
    def name(self) -> str:
        return "retry-test"

    async def health(self) -> bool:
        return True

    async def chat(self, messages: list[LLMMessage]) -> str:
        return "unused"

    async def complete(self, messages: list[LLMMessage], tools: list[dict] | None = None) -> LLMResponse:
        self.round += 1
        if self.round == 1:
            return LLMResponse(tool_calls=[LLMToolCall(function=LLMToolFunction(name="system_shell", arguments={"command": "Get-NetAdapter"}))])
        if self.round == 2:
            return LLMResponse(content="Não consegui por falta de permissão; posso tentar ipconfig?")
        if self.round == 3:
            assert tools and "READ_ONLY RETRY REQUIRED" in messages[-1].content
            return LLMResponse(tool_calls=[LLMToolCall(function=LLMToolFunction(name="system_shell", arguments={"command": "ipconfig"}))])
        return LLMResponse(content="A interface Ethernet usa 192.168.1.108.")


@pytest.mark.asyncio
async def test_system_shell_is_exposed_and_tool_loop_uses_real_result(tmp_path: Path):
    settings = Settings.from_sources(
        database_path=tmp_path / "tool-loop.db",
        shell_enabled=True,
        shell_default="powershell",
        shell_default_working_directory=tmp_path,
    )
    executor = PingExecutor()
    shell = SystemShellService(settings, EventBus(), executor=executor)
    await shell.initialize()
    registry = create_tool_registry(shell)
    definitions = {item["name"]: item for item in registry.descriptions()}
    assert definitions["system_shell"]["risk"] == "DYNAMIC"
    assert any(item["function"]["name"] == "system_shell" for item in registry.llm_tools())

    response = await ToolAgentLoop(ToolCallingLLM(), registry).run([
        LLMMessage(role="system", content="Use tools."),
        LLMMessage(role="user", content="Kazumi, pinga o Proxmox."),
    ])
    assert executor.commands == ["ping 192.168.1.2 -n 1"]
    assert "1 ms" in response


def test_central_network_alias_registry_resolves_baseline(tmp_path):
    # Controlled aliases, never the operator's private runtime registry.
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"hosts": [
        {"id":"gateway","address":"192.168.1.1","aliases":["roteador","OpenWrt"],"remote_shell":{"enabled":True}},
        {"id":"proxmox","address":"192.168.1.2","aliases":["Proxmox"],"remote_shell":{"enabled":True}},
        {"id":"dc1","address":"192.168.1.10","aliases":["controlador de domínio"]}
    ]}), encoding="utf8")
    aliases = NetworkAliasRegistry(path)
    assert aliases.resolve("roteador").address == "192.168.1.1"
    assert aliases.resolve("Proxmox").address == "192.168.1.2"
    assert aliases.resolve("controlador de domínio").address == "192.168.1.10"
    assert aliases.find_remote_in_text("Kazumi, verifica o Proxmox.").id == "proxmox"
    assert aliases.find_remote_in_text("O OpenWrt está saudável?").id == "gateway"


@pytest.mark.asyncio
async def test_grounding_guard_corrects_unsupported_permission_claim(tmp_path: Path):
    settings = Settings.from_sources(
        database_path=tmp_path / "grounding.db",
        shell_enabled=True,
        shell_default_working_directory=tmp_path,
    )
    shell = SystemShellService(settings, EventBus(), executor=PingExecutor())
    await shell.initialize()
    result = await ToolAgentLoop(HallucinatedPermissionLLM(), create_tool_registry(shell)).run([
        LLMMessage(role="user", content="Mostra minhas interfaces."),
    ])
    assert "Ethernet está ativa" in result


@pytest.mark.asyncio
async def test_read_only_permission_failure_triggers_autonomous_fallback(tmp_path: Path):
    settings = Settings.from_sources(
        database_path=tmp_path / "retry.db",
        shell_enabled=True,
        shell_default_working_directory=tmp_path,
    )
    shell = SystemShellService(settings, EventBus(), executor=FallbackExecutor())
    await shell.initialize()
    llm = RetryLLM()
    result = await ToolAgentLoop(llm, create_tool_registry(shell)).run([
        LLMMessage(role="user", content="Mostra minhas interfaces."),
    ])
    assert llm.round == 4
    assert "192.168.1.108" in result
