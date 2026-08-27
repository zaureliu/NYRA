from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from app.agent import AgentController, AgentRunState, AgentRunStatus
from app.core.config import Settings
from app.events import EventBus, EventType
from app.llm.base import LLMMessage, LLMProvider, LLMResponse, LLMToolCall, LLMToolFunction
from app.tools.models import RiskLevel, ToolResult
from app.tools.agent import ToolAgentLoop
from app.tools.registry import ToolDefinition, ToolRegistry


class CommandInput(BaseModel):
    command: str = Field(min_length=1)
    approval_id: str | None = None


def call(name: str, command: str, approval_id: str | None = None) -> LLMResponse:
    arguments = {"command": command}
    if approval_id:
        arguments["approval_id"] = approval_id
    return LLMResponse(tool_calls=[LLMToolCall(function=LLMToolFunction(name=name, arguments=arguments))])


class SequenceLLM(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.index = 0

    @property
    def name(self) -> str:
        return "agent-sequence"

    async def health(self) -> bool:
        return True

    async def chat(self, messages: list[LLMMessage]) -> str:
        return "unused"

    async def complete(self, messages: list[LLMMessage], tools=None) -> LLMResponse:
        if self.index >= len(self.responses):
            return LLMResponse(content="Relatório final baseado nas evidências disponíveis.")
        result = self.responses[self.index]
        self.index += 1
        return result


def settings(tmp_path: Path, **overrides) -> Settings:
    return Settings.from_sources(
        database_path=tmp_path / "agent.db",
        agent_enabled=True,
        agent_max_steps=overrides.pop("agent_max_steps", 12),
        agent_max_tool_calls=overrides.pop("agent_max_tool_calls", 20),
        agent_max_runtime_seconds=overrides.pop("agent_max_runtime_seconds", 30),
        agent_max_identical_repeats=overrides.pop("agent_max_identical_repeats", 2),
        agent_max_consecutive_failures=overrides.pop("agent_max_consecutive_failures", 3),
        **overrides,
    )


def registry(functions: dict[str, tuple[RiskLevel, object]]) -> ToolRegistry:
    value = ToolRegistry()
    for name, (risk, function) in functions.items():
        value.register(ToolDefinition(
            name=name, description=f"test {name}", risk=risk, input_model=CommandInput,
            function=function,
            preflight=lambda payload, risk=risk, name=name: {
                "risk_level": risk.value,
                "resource_key": f"test:{name}:{payload['command']}",
                "host": "test-host",
            },
        ))
    return value


def messages() -> list[LLMMessage]:
    return [LLMMessage(role="user", content="investigue o serviço")]


@pytest.mark.asyncio
async def test_simple_diagnosis_observe_diagnose_complete(tmp_path: Path):
    async def observe(command: str, approval_id: str | None = None):
        return {"success": True, "stdout": "service active", "exit_code": 0}

    bus = EventBus(history_size=100)
    controller = AgentController(
        settings(tmp_path), bus,
        SequenceLLM([call("observe", "status"), LLMResponse(content="O serviço está ativo.")]),
        registry({"observe": (RiskLevel.READ_ONLY, observe)}),
    )
    await controller.initialize()
    response = await controller.run(messages(), "verificar serviço")
    run = (await controller.recent(1))[0]
    states = [event.payload.get("state") for event in bus.history() if event.type == EventType.AGENT_RUN_STATE_CHANGED]
    assert response == "O serviço está ativo."
    assert run.status == AgentRunStatus.COMPLETED and run.state == AgentRunState.COMPLETE
    assert "OBSERVE" in states and "DIAGNOSE" in states and "COMPLETE" in states
    assert run.steps[0].summary == "service active"


@pytest.mark.asyncio
async def test_action_requires_read_only_verification_before_complete(tmp_path: Path):
    executed: list[str] = []

    async def tool(command: str, approval_id: str | None = None):
        executed.append(command)
        return {"success": True, "stdout": f"{command}:ok", "exit_code": 0}

    llm = SequenceLLM([
        call("observe", "status"), call("action", "restart"),
        LLMResponse(content="Resolvido."),  # rejected until verification
        call("observe", "status-after"), LLMResponse(content="Reiniciei e confirmei o estado ativo."),
    ])
    controller = AgentController(
        settings(tmp_path), EventBus(), llm,
        registry({"observe": (RiskLevel.READ_ONLY, tool), "action": (RiskLevel.LOW_RISK, tool)}),
    )
    await controller.initialize()
    response = await controller.run(messages(), "recuperar serviço")
    run = (await controller.recent(1))[0]
    assert executed == ["status", "restart", "status-after"]
    assert [step.state for step in run.steps] == [AgentRunState.OBSERVE, AgentRunState.ACT, AgentRunState.VERIFY]
    assert run.status == AgentRunStatus.COMPLETED and "confirmei" in response


@pytest.mark.asyncio
async def test_consecutive_failures_stop_controlled_retry(tmp_path: Path):
    attempts = 0

    async def failing(command: str, approval_id: str | None = None):
        nonlocal attempts
        attempts += 1
        return {"success": False, "stderr": "same failure", "exit_code": 1}

    controller = AgentController(
        settings(tmp_path, agent_max_consecutive_failures=2), EventBus(),
        SequenceLLM([call("action", "restart"), call("action", "restart"), LLMResponse(content="A correção falhou duas vezes; interrompi.")]),
        registry({"action": (RiskLevel.LOW_RISK, failing)}),
    )
    await controller.initialize()
    await controller.run(messages(), "recuperar serviço")
    run = (await controller.recent(1))[0]
    assert attempts == 2
    assert run.status == AgentRunStatus.FAILED and run.error == "AGENT_CONSECUTIVE_FAILURES"


@pytest.mark.asyncio
async def test_approval_waits_and_resumes_same_agent_run(tmp_path: Path):
    approval_id = "apr_test_approval_1234567890"
    executed: list[str] = []

    async def action(command: str, approval_id: str | None = None):
        if not approval_id:
            return {"success": False, "error_code": "APPROVAL_REQUIRED", "approval_id": globals_approval_id}
        executed.append(command)
        return {"success": True, "stdout": "restarted", "exit_code": 0, "approval_granted": True}

    async def observe(command: str, approval_id: str | None = None):
        executed.append(command)
        return {"success": True, "stdout": "active", "exit_code": 0}

    globals_approval_id = approval_id
    llm = SequenceLLM([
        call("action", "restart"), LLMResponse(content=f"Aguardando autorização {approval_id}."),
        call("action", "restart", approval_id), call("observe", "verify"),
        LLMResponse(content="Ação aprovada, executada e verificada."),
    ])
    controller = AgentController(
        settings(tmp_path), EventBus(), llm,
        registry({"action": (RiskLevel.ELEVATED, action), "observe": (RiskLevel.READ_ONLY, observe)}),
    )
    await controller.initialize()
    first = await controller.run(messages(), "recuperar serviço")
    waiting = (await controller.recent(1))[0]
    assert waiting.status == AgentRunStatus.WAITING_APPROVAL and waiting.pending_approval_id == approval_id
    assert "Aguardando" in first

    second = await controller.run(messages(), "recuperar serviço", resume_run_id=waiting.id)
    resumed = await controller.get(waiting.id)
    assert resumed.id == waiting.id and resumed.status == AgentRunStatus.COMPLETED
    assert executed == ["restart", "verify"] and "verificada" in second


@pytest.mark.asyncio
async def test_tool_call_limit_stops_loop(tmp_path: Path):
    calls = 0

    async def observe(command: str, approval_id: str | None = None):
        nonlocal calls
        calls += 1
        return {"success": True, "stdout": command, "exit_code": 0}

    controller = AgentController(
        settings(tmp_path, agent_max_tool_calls=2), EventBus(),
        SequenceLLM([call("observe", "one"), call("observe", "two"), call("observe", "three"), LLMResponse(content="Limite atingido.")]),
        registry({"observe": (RiskLevel.READ_ONLY, observe)}),
    )
    await controller.initialize()
    await controller.run(messages(), "loop limitado")
    run = (await controller.recent(1))[0]
    assert calls == 2 and run.status == AgentRunStatus.FAILED and run.error == "AGENT_TOOL_CALL_LIMIT"


@pytest.mark.asyncio
async def test_identical_command_and_result_stop_no_progress(tmp_path: Path):
    calls = 0

    async def observe(command: str, approval_id: str | None = None):
        nonlocal calls
        calls += 1
        return {"success": True, "stdout": "unchanged", "exit_code": 0}

    controller = AgentController(
        settings(tmp_path, agent_max_identical_repeats=2), EventBus(),
        SequenceLLM([call("observe", "ping"), call("observe", "ping"), call("observe", "ping"), LLMResponse(content="Sem progresso; interrompi.")]),
        registry({"observe": (RiskLevel.READ_ONLY, observe)}),
    )
    await controller.initialize()
    await controller.run(messages(), "detectar repetição")
    run = (await controller.recent(1))[0]
    assert calls == 3 and run.status == AgentRunStatus.FAILED and run.error == "AGENT_NO_PROGRESS"


@pytest.mark.asyncio
async def test_read_only_mode_blocks_mutation(tmp_path: Path):
    """Semântica PRO: read_only bloqueia ELEVATED+, mas permite LOW_RISK."""
    calls: list[str] = []

    async def action(command: str, approval_id: str | None = None):
        calls.append("action")
        return {"success": True}

    async def elevated_action(command: str, approval_id: str | None = None):
        calls.append("elevated_action")
        return {"success": True}

    async def check_state(command: str = "", approval_id: str | None = None):
        return {"success": True, "state": "verified"}

    controller = AgentController(
        settings(tmp_path, agent_read_only=True), EventBus(),
        SequenceLLM([
            call("action", "reinicio seguro"),
            call("elevated_action", "shutdown"),
            call("check_state", "estado atual"),
            LLMResponse(content="Ação LOW_RISK executada e verificada; a alteração elevada foi bloqueada pelo modo read-only."),
        ]),
        registry({
            "action": (RiskLevel.LOW_RISK, action),
            "elevated_action": (RiskLevel.ELEVATED, elevated_action),
            "check_state": (RiskLevel.READ_ONLY, check_state),
        }),
    )
    await controller.initialize()
    response = await controller.run(messages(), "executar manutenção")
    assert "action" in calls and "elevated_action" not in calls
    assert "read-only" in response


@pytest.mark.asyncio
async def test_agent_run_cancellation_marks_cancelled(tmp_path: Path):
    started = asyncio.Event()

    async def waiting(command: str, approval_id: str | None = None):
        started.set()
        await asyncio.Future()

    controller = AgentController(
        settings(tmp_path), EventBus(), SequenceLLM([call("observe", "wait")]),
        registry({"observe": (RiskLevel.READ_ONLY, waiting)}),
    )
    await controller.initialize()
    task = asyncio.create_task(controller.run(messages(), "cancelar investigação"))
    await asyncio.wait_for(started.wait(), 2)
    run_id = next(iter(controller._runs))
    assert await controller.cancel(run_id) is True
    with pytest.raises(asyncio.CancelledError):
        await task
    run = await controller.get(run_id)
    assert run.status == AgentRunStatus.CANCELLED and run.state == AgentRunState.CANCELLED


@pytest.mark.asyncio
async def test_remote_execution_requires_local_network_precheck_per_registered_address(tmp_path: Path):
    executed: list[str] = []

    async def local(command: str, approval_id: str | None = None):
        executed.append(f"local:{command}")
        return {"success": True, "command": command, "stdout": "reply", "exit_code": 0}

    async def remote(command: str, approval_id: str | None = None):
        executed.append(f"remote:{command}")
        return {"success": True, "host": "proxmox", "command": command, "stdout": "up", "exit_code": 0}

    tools = ToolRegistry()
    tools.register(ToolDefinition(
        "system_shell", "local", RiskLevel.READ_ONLY, CommandInput, local,
        preflight=lambda payload: {"risk_level": "READ_ONLY", "resource_key": "local:network", "host": "local"},
    ))
    tools.register(ToolDefinition(
        "remote_shell", "remote", RiskLevel.READ_ONLY, CommandInput, remote,
        preflight=lambda payload: {"risk_level": "READ_ONLY", "resource_key": "remote:proxmox", "host": "proxmox", "address": "192.168.1.2"},
    ))
    llm = SequenceLLM([
        call("remote_shell", "uptime"),
        call("system_shell", "ping 192.168.1.2"),
        call("remote_shell", "uptime"),
        LLMResponse(content="Conectividade e SSH verificados."),
    ])
    controller = AgentController(settings(tmp_path), EventBus(), llm, tools)
    await controller.initialize()
    response = await controller.run(messages(), "verificar Proxmox")
    assert executed == ["local:ping 192.168.1.2", "remote:uptime"]
    assert "verificados" in response


@pytest.mark.asyncio
async def test_textual_tool_call_is_never_executed_and_model_must_retry_native(tmp_path: Path):
    executed: list[str] = []

    async def observe(command: str, approval_id: str | None = None):
        executed.append(command)
        return {"success": True, "stdout": "real", "exit_code": 0}

    llm = SequenceLLM([
        LLMResponse(content='<tool_call>{"name":"system_shell","arguments":{"command":"fake"}}</tool_call>'),
        call("observe", "native"),
        LLMResponse(content="Usei apenas o resultado real."),
    ])
    controller = AgentController(
        settings(tmp_path), EventBus(), llm,
        registry({"observe": (RiskLevel.READ_ONLY, observe)}),
    )
    await controller.initialize()
    response = await controller.run(messages(), "não executar texto")
    assert executed == ["native"]
    assert response == "Usei apenas o resultado real."


@pytest.mark.asyncio
async def test_local_nyra_backend_goal_rejects_homelab_target_and_requires_two_observations(tmp_path: Path):
    executed: list[str] = []

    async def local(command: str, approval_id: str | None = None):
        executed.append(command)
        return {"success": True, "command": command, "stdout": "observed", "exit_code": 0}

    tools = ToolRegistry()
    tools.register(ToolDefinition(
        "system_shell", "local", RiskLevel.READ_ONLY, CommandInput, local,
        preflight=lambda payload: {"risk_level": "READ_ONLY", "resource_key": "local:backend", "host": "local"},
    ))
    llm = SequenceLLM([
        call("system_shell", "Test-NetConnection 192.168.1.1 -Port 22"),
        call("system_shell", "Test-NetConnection 127.0.0.1 -Port 8000"),
        LLMResponse(content="O backend está parado."),
        call("system_shell", "Get-Process python"),
        LLMResponse(content="Inspecionei porta e processo locais."),
    ])
    controller = AgentController(settings(tmp_path), EventBus(), llm, tools)
    await controller.initialize()
    response = await controller.run(messages(), "verifica por que o backend da NYRA caiu")
    assert executed == ["Test-NetConnection 127.0.0.1 -Port 8000", "Get-Process python"]
    assert "porta e processo" in response


def test_auth_failure_grounding_rejects_password_or_key_hunting_promises():
    results = [
        ToolResult(tool="system_shell", risk=RiskLevel.READ_ONLY, ok=True, data={"success": True, "stdout": "reply"}, elapsed_ms=1),
        ToolResult(tool="remote_shell", risk=RiskLevel.READ_ONLY, ok=False, data={"success": False, "error_code": "SSH_AUTHENTICATION_FAILED"}, elapsed_ms=1),
    ]
    draft = "Verificarei se há uma chave configurada ou solicitarei a senha para tentar novamente."
    assert ToolAgentLoop._needs_grounding_correction(draft, results) is True
    assert "nenhuma senha" in ToolAgentLoop._safe_grounding_fallback(results)


@pytest.mark.asyncio
async def test_unsafe_grounding_rewrite_falls_back_to_deterministic_auth_report():
    unsafe = "Vou verificar a chave e pedir a senha para tentar novamente."
    loop = ToolAgentLoop(SequenceLLM([LLMResponse(content=unsafe)]), ToolRegistry())
    results = [
        ToolResult(tool="remote_shell", risk=RiskLevel.READ_ONLY, ok=False, data={"success": False, "error_code": "SSH_AUTHENTICATION_FAILED"}, elapsed_ms=1),
    ]
    value = await loop._grounded_final([LLMMessage(role="user", content="verifique")], unsafe, results)
    assert "nenhuma senha" in value and "Trusted Host Registry" in value


def test_future_tense_report_and_read_only_action_get_deterministic_closure():
    results = [
        ToolResult(tool="system_shell", risk=RiskLevel.READ_ONLY, ok=True, data={"success": True, "stdout": "port checked"}, elapsed_ms=1),
        ToolResult(tool="system_shell", risk=RiskLevel.ELEVATED, ok=False, data={"success": False, "error_code": "AGENT_READ_ONLY"}, elapsed_ms=1),
    ]
    assert ToolAgentLoop._needs_grounding_correction("Vou inspecionar os logs agora.", results) is True
    assert "Nenhuma mudança" in ToolAgentLoop._safe_grounding_fallback(results)
    listening = [ToolResult(
        tool="system_shell", risk=RiskLevel.READ_ONLY, ok=True,
        data={"success": True, "command": "netstat -ano | findstr :8000", "stdout": "TCP 127.0.0.1:8000 0.0.0.0:0 LISTENING 12028"},
        elapsed_ms=1,
    )]
    assert "PID 12028" in ToolAgentLoop._safe_grounding_fallback(listening)
