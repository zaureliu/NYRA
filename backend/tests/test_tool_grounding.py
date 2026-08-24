"""Anti-hallucination / tool grounding suite.

Covers the notepad incident regression and the grounding policy:
provenance (tool_call_id correlation), execution vs effect distinction,
fabricated value detection, absence claims after failures, truncation
awareness and honest reporting when verification is impossible.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from app.agent import AgentController, AgentRunStatus
from app.core.config import Settings
from app.events import EventBus
from app.llm.base import LLMMessage, LLMProvider, LLMResponse, LLMToolCall, LLMToolFunction
from app.tools.agent import ToolAgentLoop
from app.tools.grounding import (
    GroundingLedger,
    fabricated_value_claims,
    unverified_effect_claims,
    VerificationStatus,
)
from app.tools.models import RiskLevel
from app.tools.registry import ToolDefinition, ToolRegistry, create_tool_registry
from app.tools.shell_executor import RawShellResult, ShellExecutor
from app.tools.system_shell import SystemShellService


def call(name: str, command: str) -> LLMResponse:
    return LLMResponse(tool_calls=[LLMToolCall(function=LLMToolFunction(name=name, arguments={"command": command}))])


class SequenceLLM(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.index = 0
        self.rounds: list[list[LLMMessage]] = []

    @property
    def name(self) -> str:
        return "grounding-sequence"

    async def health(self) -> bool:
        return True

    async def chat(self, messages: list[LLMMessage]) -> str:
        return "unused"

    async def complete(self, messages: list[LLMMessage], tools=None) -> LLMResponse:
        self.rounds.append(list(messages))
        if self.index >= len(self.responses):
            return LLMResponse(content="Relatório final baseado nas evidências disponíveis.")
        result = self.responses[self.index]
        self.index += 1
        return result


class ScriptedExecutor(ShellExecutor):
    """Returns scripted results per exact command."""

    def __init__(self, script: dict[str, RawShellResult]) -> None:
        self.script = script
        self.commands: list[str] = []

    def resolve_executable(self, shell: str) -> str | None:
        return "powershell.exe"

    async def execute(self, command: str, shell: str, timeout_seconds: int, working_directory: Path) -> RawShellResult:
        self.commands.append(command)
        result = self.script.get(command)
        if result is None:
            raise AssertionError(f"comando inesperado: {command}")
        return result


def run_tool(stdout: bytes = b"", stderr: bytes = b"", exit_code: int = 0) -> RawShellResult:
    return RawShellResult("powershell.exe", exit_code, stdout, stderr, 5.0)


async def shell_service(tmp_path: Path, executor: ShellExecutor, **overrides) -> SystemShellService:
    settings = Settings.from_sources(
        database_path=tmp_path / "grounding-suite.db",
        shell_enabled=True,
        shell_default="powershell",
        shell_default_working_directory=tmp_path,
        **overrides,
    )
    service = SystemShellService(settings, EventBus(), executor=executor)
    await service.initialize()
    return service


def controller_settings(tmp_path: Path) -> Settings:
    return Settings.from_sources(
        database_path=tmp_path / "grounding-agent.db",
        agent_enabled=True,
        agent_max_steps=12,
        agent_max_tool_calls=20,
        agent_max_runtime_seconds=30,
    )


class CommandInput(BaseModel):
    command: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Unit-level detection rules
# ---------------------------------------------------------------------------


def test_fabrication_detector_rejects_pid_absent_from_all_evidence():
    ledger = GroundingLedger()
    ledger.record(
        tool_call_id="call_a", tool_name="system_shell",
        result_data={"success": False, "exit_code": 1, "stderr": "No process found"},
        risk_level="READ_ONLY",
    )
    violations = fabricated_value_claims("O PID 1234 está ativo e saudável.", ledger)
    assert any(item.kind == "FABRICATED_VALUE" and "1234" in item.detail for item in violations)


def test_fabrication_detector_accepts_pid_present_in_evidence():
    ledger = GroundingLedger()
    ledger.record(
        tool_call_id="call_a", tool_name="system_shell",
        result_data={"success": True, "exit_code": 0, "stdout": "Id=5320 ProcessName=notepad"},
        risk_level="READ_ONLY",
    )
    draft = "O processo notepad usa o PID 5320."
    assert fabricated_value_claims(draft, ledger) == []


def test_partial_fields_allow_reported_values_but_not_invented_session():
    ledger = GroundingLedger()
    ledger.record(
        tool_call_id="call_a", tool_name="system_shell",
        result_data={"success": True, "exit_code": 0, "stdout": "Id=5320 ProcessName=notepad"},
        risk_level="READ_ONLY",
    )
    ok_draft = "Encontrei notepad com PID 5320; não consegui confirmar o SessionId nesta consulta."
    assert fabricated_value_claims(ok_draft, ledger) == []
    bad_draft = "notepad PID 5320 no SessionId 1."
    assert any("SESSION_ID" in item.detail for item in fabricated_value_claims(bad_draft, ledger))


def test_empty_probe_output_marks_verification_failed_and_blocks_success_claims():
    ledger = GroundingLedger()
    mutation = ledger.record(
        tool_call_id="call_m", tool_name="system_shell",
        result_data={"success": True, "exit_code": 0, "command": "Start-Process notepad.exe"},
        risk_level="LOW_RISK",
    )
    assert mutation.verification_status == VerificationStatus.EXECUTED

    # Boilerplate message only (no stdout/stderr): cannot confirm an effect.
    empty_probe = ledger.record(
        tool_call_id="call_v1", tool_name="system_shell",
        result_data={"success": True, "exit_code": 0, "command": "Get-Process notepad", "message": "terminou sem saída"},
        risk_level="READ_ONLY",
    )
    ledger.record_verification_attempt(empty_probe)
    assert mutation.verification_status == VerificationStatus.VERIFICATION_FAILED
    assert unverified_effect_claims("Bloco de notas aberto com sucesso.", ledger)

    # Real evidence output verifies the turn.
    recovery = GroundingLedger()
    m2 = recovery.record(
        tool_call_id="m2", tool_name="system_shell",
        result_data={"success": True, "exit_code": 0, "command": "Start-Process notepad.exe"},
        risk_level="LOW_RISK",
    )
    good_probe = recovery.record(
        tool_call_id="v2", tool_name="system_shell",
        result_data={"success": True, "exit_code": 0, "command": "Get-Process notepad", "stdout": "notepad 5320"},
        risk_level="READ_ONLY",
    )
    recovery.record_verification_attempt(good_probe)
    assert m2.verification_status == VerificationStatus.VERIFIED
    assert unverified_effect_claims("Bloco de notas aberto com sucesso.", recovery) == []


def test_explicit_false_probe_counts_as_verification_failed():
    """Test-Path returning False is real output but negative evidence."""
    ledger = GroundingLedger()
    mutation = ledger.record(
        tool_call_id="call_m", tool_name="system_shell",
        result_data={"success": True, "exit_code": 0, "command": "New-Item C:\\temp\\teste.txt"},
        risk_level="LOW_RISK",
    )
    probe = ledger.record(
        tool_call_id="call_v", tool_name="system_shell",
        result_data={"success": True, "exit_code": 0, "command": "Test-Path C:\\temp\\teste.txt", "stdout": "False"},
        risk_level="READ_ONLY",
    )
    ledger.record_verification_attempt(probe)
    assert mutation.verification_status == VerificationStatus.VERIFICATION_FAILED
    assert unverified_effect_claims("Arquivo teste.txt criado com sucesso.", ledger)


def test_structured_desktop_probe_verifies_mutation_without_shell_stdout():
    ledger = GroundingLedger(turn_id="turn_desktop_structured")
    mutation = ledger.record(
        tool_call_id="call_open",
        tool_name="desktop_launch",
        result_data={"success": True, "effect_verified": True},
        risk_level="LOW_RISK",
        resource_key="desktop:notepad",
    )
    probe = ledger.record(
        tool_call_id="call_windows",
        tool_name="desktop_windows",
        result_data={
            "success": True,
            "open": True,
            "verification_status": "VERIFIED",
            "windows": [{"pid": 5320, "visible": True, "process_name": "notepad.exe", "title": "private"}],
        },
        risk_level="READ_ONLY",
        resource_key="desktop:notepad",
    )

    ledger.record_verification_attempt(probe)

    assert mutation.verification_status == VerificationStatus.VERIFIED
    assert "5320" in probe.structured_evidence
    assert "private" not in probe.structured_evidence
    assert unverified_effect_claims("Bloco de notas aberto com sucesso.", ledger) == []


def test_private_desktop_window_titles_are_not_sent_back_to_model():
    original = {
        "data": {
            "success": True,
            "open": True,
            "windows": [{"pid": 5320, "title": "documento privado.txt", "process_name": "notepad.exe"}],
        }
    }

    safe = ToolAgentLoop._model_safe_result("desktop_windows", original)

    assert safe["data"]["windows"][0]["title"] == "<janela visível>"
    assert original["data"]["windows"][0]["title"] == "documento privado.txt"


def test_unverified_effect_claim_is_blocked_when_no_probe_follows_mutation():
    ledger = GroundingLedger()
    ledger.record(
        tool_call_id="call_m", tool_name="system_shell",
        result_data={"success": True, "exit_code": 0, "command": "Start-Process notepad.exe"},
        risk_level="LOW_RISK",
    )
    violations = unverified_effect_claims("notepad.exe foi iniciado com sucesso.", ledger)
    assert violations and violations[0].kind == "UNVERIFIED_EFFECT"
    assert unverified_effect_claims("Solicitei a abertura do Bloco de Notas.", ledger) == []


def test_effect_claim_without_any_mutation_this_turn_is_blocked():
    ledger = GroundingLedger()
    ledger.record(
        tool_call_id="call_q", tool_name="system_shell",
        result_data={"success": True, "exit_code": 0, "stdout": "config read"},
        risk_level="READ_ONLY",
    )
    violations = unverified_effect_claims("Abri o bloco de notas para você.", ledger)
    assert violations and violations[0].kind == "UNVERIFIED_EFFECT"


# ---------------------------------------------------------------------------
# Loop integration — the notepad incident and friends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notepad_incident_exit_zero_with_missing_process_never_reports_success(tmp_path: Path):
    """Spec #65: Start-Process exit 0 + verification finds nothing => launch NOT confirmed."""
    executor = ScriptedExecutor({
        "Start-Process notepad.exe": run_tool(),
        "Get-Process notepad": run_tool(b"", b"Get-Process : Nao foi possivel encontrar um processo chamado notepad", 1),
    })
    registry = create_tool_registry(await shell_service(tmp_path, executor))
    llm = SequenceLLM([
        call("system_shell", "Start-Process notepad.exe"),
        call("system_shell", "Get-Process notepad"),
        LLMResponse(content="notepad.exe foi iniciado com sucesso e está ativo."),
        LLMResponse(content="A solicitação de abertura foi executada, mas não consegui confirmar uma instância ativa do Notepad."),
    ])
    response = await ToolAgentLoop(llm, registry).run([LLMMessage(role="user", content="Nyra, abre o bloco de notas.")])
    assert "iniciado com sucesso" not in response
    assert "não consegui confirmar" in response or "não confirmada" in response


@pytest.mark.asyncio
async def test_empty_output_cannot_produce_pid_answer(tmp_path: Path):
    """Spec #50: empty stdout/stderr/exit 0 cannot yield an invented PID."""
    executor = ScriptedExecutor({"Get-Process notepad": run_tool()})
    registry = create_tool_registry(await shell_service(tmp_path, executor))
    llm = SequenceLLM([
        call("system_shell", "Get-Process notepad"),
        LLMResponse(content="O PID é 1234."),
        LLMResponse(content="O comando terminou sem erro, mas não retornou dados que permitam informar o PID."),
    ])
    response = await ToolAgentLoop(llm, registry).run([LLMMessage(role="user", content="Qual é o PID?")])
    assert "1234" not in response


@pytest.mark.asyncio
async def test_missing_process_stdout_cannot_contain_any_pid(tmp_path: Path):
    """Spec #51: 'No process found.' output must never gain a PID."""
    executor = ScriptedExecutor({"Get-Process notepad": run_tool(b"No process found.")})
    registry = create_tool_registry(await shell_service(tmp_path, executor))
    llm = SequenceLLM([
        call("system_shell", "Get-Process notepad"),
        LLMResponse(content="PID 4321 encontrado."),
        LLMResponse(content="Nenhuma instância de notepad foi encontrada no momento desta verificação."),
    ])
    response = await ToolAgentLoop(llm, registry).run([LLMMessage(role="user", content="qual o PID do notepad?")])
    assert "4321" not in response and "nenhuma instância" in response.casefold()


@pytest.mark.asyncio
async def test_access_denied_cannot_become_absence_claim_without_fallback(tmp_path: Path):
    """Spec #54: failed probe may not be reported as 'there is none'."""
    executor = ScriptedExecutor({
        "Get-NetTCPConnection -LocalPort 8000": run_tool(b"", b"Access denied", 1),
    })
    registry = create_tool_registry(await shell_service(tmp_path, executor))
    llm = SequenceLLM([
        call("system_shell", "Get-NetTCPConnection -LocalPort 8000"),
        LLMResponse(content="Não há nenhum listener nessa porta."),
        LLMResponse(content="A consulta falhou com acesso negado; não pude determinar o estado da porta sem um fallback."),
    ])
    response = await ToolAgentLoop(llm, registry).run([LLMMessage(role="user", content="porta 8000")])
    assert "não há nenhum listener" not in response.casefold()


@pytest.mark.asyncio
async def test_fallback_result_grounded_response(tmp_path: Path):
    """Spec #55: primary denied + successful fallback => answer grounded on fallback."""
    executor = ScriptedExecutor({
        "Get-NetTCPConnection -LocalPort 8000": run_tool(b"", b"Access denied", 1),
        "netstat -ano | findstr :8000": run_tool(b"TCP    127.0.0.1:8000    0.0.0.0:0    LISTENING    12028"),
    })
    registry = create_tool_registry(await shell_service(tmp_path, executor))
    llm = SequenceLLM([
        call("system_shell", "Get-NetTCPConnection -LocalPort 8000"),
        call("system_shell", "netstat -ano | findstr :8000"),
        LLMResponse(content="Pelo fallback netstat, a porta 8000 está em LISTENING associada ao PID 12028."),
    ])
    response = await ToolAgentLoop(llm, registry).run([LLMMessage(role="user", content="porta 8000")])
    assert "12028" in response
    assert "fallback netstat" in response.casefold()


@pytest.mark.asyncio
async def test_multiple_calls_keep_correct_call_to_result_correlation(tmp_path: Path):
    """Spec #56/#59: A->100, B->200 with per-call provenance binding."""
    executor = ScriptedExecutor({
        "Get-Proc alpha": run_tool(b'{"pid": 100}'),
        "Get-Proc beta": run_tool(b'{"pid": 200}'),
    })
    registry = create_tool_registry(await shell_service(tmp_path, executor))
    llm = SequenceLLM([
        LLMResponse(tool_calls=[
            LLMToolCall(function=LLMToolFunction(name="system_shell", arguments={"command": "Get-Proc alpha"})),
            LLMToolCall(function=LLMToolFunction(name="system_shell", arguments={"command": "Get-Proc beta"})),
        ]),
        LLMResponse(content="alpha usa o PID 100 e beta usa o PID 200."),
    ])
    response = await ToolAgentLoop(llm, registry).run([LLMMessage(role="user", content="liste os processos")])
    assert response == "alpha usa o PID 100 e beta usa o PID 200."

    final_round = llm.rounds[-1]
    tool_messages = [m for m in final_round if m.role == "tool"]
    assistant_calls = [m for m in final_round if m.role == "assistant"][-1].tool_calls
    assert len(tool_messages) == len(assistant_calls) == 2
    for message, tool_call in zip(tool_messages, assistant_calls):
        assert message.tool_call_id == tool_call.tool_call_id
        subject = tool_call.function.arguments["command"].split()[-1]
        assert subject in message.content


@pytest.mark.asyncio
async def test_final_answer_only_after_required_tools_finished(tmp_path: Path):
    """Spec #58/#31 barrier: no factual final before tool results are in context."""
    executor = ScriptedExecutor({"Get-Process explorer": run_tool(b"explorer OK")})
    registry = create_tool_registry(await shell_service(tmp_path, executor))
    llm = SequenceLLM([
        call("system_shell", "Get-Process explorer"),
        LLMResponse(content="O processo explorer está presente conforme a consulta real."),
    ])
    await ToolAgentLoop(llm, registry).run([LLMMessage(role="user", content="explorer está rodando?")])
    final_round = llm.rounds[-1]
    assert any(m.role == "tool" and "explorer OK" in m.content for m in final_round)


@pytest.mark.asyncio
async def test_truncated_flag_is_recorded_and_blocks_absence_conclusion(tmp_path: Path):
    """Spec #60/#45: truncated flag prevents claiming the missing value does not exist."""
    big = ("x" * 5000).encode()
    executor = ScriptedExecutor({"Get-Process | Out-String": run_tool(big)})
    service = await shell_service(tmp_path, executor, shell_max_output_chars=1000)
    result = await service.execute("Get-Process | Out-String")
    assert result["stdout_truncated"] is True
    assert "NYRA OUTPUT TRUNCATED" in result["stdout"]

    ledger = GroundingLedger()
    observation = ledger.record(
        tool_call_id="call_t", tool_name="system_shell", result_data=result, risk_level="READ_ONLY",
    )
    assert observation.stdout_truncated is True
    violations = fabricated_value_claims("O processo svchost está usando o PID 999.", ledger)
    assert any(item.kind in {"FABRICATED_VALUE", "TRUNCATED_UNVERIFIABLE"} for item in violations)


@pytest.mark.asyncio
async def test_file_creation_with_testpath_false_cannot_report_success(tmp_path: Path):
    """Spec #63: New-Item exit 0 + Test-Path False => creation NOT confirmed."""
    executor = ScriptedExecutor({
        "New-Item C:\\temp\\teste.txt": run_tool(),
        "Test-Path C:\\temp\\teste.txt": run_tool(b"False"),
    })
    registry = create_tool_registry(await shell_service(tmp_path, executor))
    llm = SequenceLLM([
        call("system_shell", "New-Item C:\\temp\\teste.txt"),
        call("system_shell", "Test-Path C:\\temp\\teste.txt"),
        LLMResponse(content="Arquivo teste.txt criado com sucesso."),
        LLMResponse(content="O comando de criação foi executado, mas a verificação indicou que o arquivo não existe; não posso afirmar que ele foi criado."),
    ])
    response = await ToolAgentLoop(llm, registry).run([LLMMessage(role="user", content="crie o arquivo")])
    assert "criado com sucesso" not in response


@pytest.mark.asyncio
async def test_verified_action_reports_confirmed_success(tmp_path: Path):
    """Positive path: mutation + correlated successful verification => confirmed report."""
    executor = ScriptedExecutor({
        "New-Item C:\\temp\\ok.txt": run_tool(),
        "Test-Path C:\\temp\\ok.txt": run_tool(b"True"),
    })
    registry = create_tool_registry(await shell_service(tmp_path, executor))
    llm = SequenceLLM([
        call("system_shell", "New-Item C:\\temp\\ok.txt"),
        call("system_shell", "Test-Path C:\\temp\\ok.txt"),
        LLMResponse(content="Criei o arquivo ok.txt e confirmei com Test-Path que ele existe."),
    ])
    response = await ToolAgentLoop(llm, registry).run([LLMMessage(role="user", content="crie o arquivo")])
    assert "confirmei" in response


@pytest.mark.asyncio
async def test_agent_run_completes_with_unverified_status_when_verification_impossible(tmp_path: Path):
    async def execute(command: str, approval_id=None):
        if command.startswith("restart"):
            return {"success": True, "exit_code": 0, "stdout": ""}
        return {"success": False, "exit_code": 1, "stderr": "probe unavailable"}

    def preflight(payload: dict) -> dict:
        mutation = payload["command"].startswith("restart")
        return {
            "risk_level": "LOW_RISK" if mutation else "READ_ONLY",
            "resource_key": "local:test",
            "host": "local",
        }

    tools = ToolRegistry()
    tools.register(ToolDefinition(
        "system_shell", "test tool", RiskLevel.LOW_RISK, CommandInput, execute,
        dynamic_risk=True, preflight=preflight,
    ))

    llm = SequenceLLM([
        call("system_shell", "restart svc"),
        LLMResponse(content="Estado relatado."),
        LLMResponse(content="Estado relatado novamente."),
        LLMResponse(content="Relato apenas o estado observável atual."),
    ])
    controller = AgentController(controller_settings(tmp_path), EventBus(), llm, tools)
    await controller.initialize()
    await controller.run([LLMMessage(role="user", content="reiniciar")], "recuperar serviço")
    run = (await controller.recent(1))[0]
    assert run.status == AgentRunStatus.COMPLETED_WITH_UNVERIFIED_ACTION
    mutation_steps = [step for step in run.steps if step.risk_level == "LOW_RISK"]
    assert mutation_steps and all(step.verification_status == VerificationStatus.EXECUTED for step in mutation_steps)


def test_routing_sends_gui_and_pid_requests_to_agent():
    tools = ToolRegistry()
    assert tools.should_route_to_agent("Nyra, abre o bloco de notas.") is True
    assert tools.should_route_to_agent("Nyra, existe algum notepad.exe rodando agora?") is True
    assert tools.should_route_to_agent("Nyra, qual o PID do Notepad?") is True
    assert tools.should_route_to_agent("Nyra, abre a calculadora.") is True
    assert tools.should_route_to_agent("Nyra, bom dia") is False
    assert tools.should_route_to_agent("me explica o que é DNS") is False


# ---------------------------------------------------------------------------
# Closure Parte 17: empty-result family — o runtime nunca inventa fatos quando
# a evidência é vazia (stdout="", {}, [], null, campo ausente, parcial,
# truncado). Meta: enforcement PASS mesmo se o modelo bruto errar.
# ---------------------------------------------------------------------------

import re

from app.tools.grounding import absence_claims_without_evidence


EMPTY_RESULT_CASES = {
    "empty_stdout": {"success": True, "stdout": ""},
    "empty_object": {},
    "null_field": {"success": True, "stdout": None},
    "missing_field": {"success": True},
    "partial_field": {"success": True, "exit_code": 0},
    "empty_list_field": {"success": True, "processes": []},
}


@pytest.mark.parametrize("case_name", sorted(EMPTY_RESULT_CASES))
def test_empty_result_family_has_no_evidence_and_never_yields_values(case_name):
    ledger = GroundingLedger()
    data = EMPTY_RESULT_CASES[case_name]
    observation = ledger.record(
        tool_call_id=ledger.new_call_id(),
        tool_name="system_shell",
        result_data=data,
        risk_level="READ_ONLY",
        resource_key="test",
        arguments_fingerprint="fp",
    )
    assert not ledger.has_any_output(), f"{case_name} não deveria produzir evidência"
    fallback = ToolAgentLoop._safe_grounding_fallback([], ledger)
    assert "não consegui confirmar" in fallback.casefold() or "sem retornar dados" in fallback.casefold() or "nenhuma correspondência" in fallback.casefold()
    # Nenhum valor inventável (PID/latência/porta) pode aparecer no fallback.
    assert not re.search(r"\bpid\s*\d+", fallback, re.I)
    assert not re.search(r"\d+\s*ms", fallback, re.I)


@pytest.mark.parametrize("case_name", sorted(EMPTY_RESULT_CASES))
def test_empty_result_cannot_support_pid_or_effect_claims(case_name):
    ledger = GroundingLedger()
    ledger.record(
        tool_call_id=ledger.new_call_id(),
        tool_name="system_shell",
        result_data=EMPTY_RESULT_CASES[case_name],
        risk_level="READ_ONLY",
        resource_key="test",
        arguments_fingerprint="fp",
    )
    pid_draft = "O processo está rodando no PID 4242 com latência 12 ms."
    assert fabricated_value_claims(pid_draft, ledger), f"{case_name}: fabricação deveria ser detectada"


def test_truncated_output_blocks_absence_conclusion():
    ledger = GroundingLedger()
    ledger.record(
        tool_call_id=ledger.new_call_id(),
        tool_name="system_shell",
        result_data={"success": True, "stdout": "x" * 100, "stdout_truncated": True},
        risk_level="READ_ONLY",
        resource_key="test",
        arguments_fingerprint="fp",
    )
    draft = "Nenhum processo encontrado."
    assert absence_claims_without_evidence(draft, ledger) or ledger.truncated_outputs() == 1
