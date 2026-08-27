"""Turn Isolation Suite: cross-turn leakage is impossible by design.

Every test drives the REAL pipeline (RealtimeOrchestrator -> tools/agent ->
GroundingLedger -> TTS boundary -> events) with scripted providers, mirroring
the operator-visible behavior: a greeting must never reuse any previous turn's
content, and every tool observation belongs to exactly one turn.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.controller import AgentController
from app.agent.models import AgentRunState, AgentRunStatus
from app.character.state import StateMachine
from app.core.config import Settings
from app.core.turn import (
    CROSS_TURN_OBSERVATION_REJECTED,
    CrossTurnObservationError,
    TurnContext,
    TurnRegistry,
    TurnStatus,
)
from app.desktop.discovery import ApplicationCandidate, score_match
from app.events import EventBus, EventType
from app.llm.base import LLMMessage, LLMResponse, LLMToolCall, LLMToolFunction
from app.memory import MemoryRepository
from app.realtime.orchestrator import RealtimeOrchestrator
from app.realtime.settings import V4SettingsManager
from app.realtime.telemetry import RealtimeTelemetry
from app.avatar import AvatarController
from app.speech.queue import SpeechQueue
from app.speech.tts import DisabledTTS
from app.tools.agent import ToolAgentLoop
from app.tools.elevated_broker import build_elevated_script, is_access_denied_output
from app.tools.grounding import GroundingLedger
from app.tools.models import RiskLevel, ToolResult
from app.tools.shell_approval import ShellApprovalGate


# --------------------------------------------------------------------- fakes


class ScriptedLLM:
    """Deterministic provider: streams scripted chat answers, pops completions."""

    name = "scripted"

    def __init__(self, chat_scripts: list[str], complete_scripts: list[LLMResponse] | None = None,
                 *, delay: float = 0.0, chunk_size: int | None = None) -> None:
        self.chat_scripts = list(chat_scripts)
        self.complete_scripts = list(complete_scripts or [])
        self.delay = delay
        self.chunk_size = chunk_size
        self.chat_calls = 0
        self.last_prompt: list[LLMMessage] = []

    async def health(self) -> bool:
        return True

    async def ready(self) -> bool:
        return True

    async def chat(self, messages: list[LLMMessage]) -> str:
        self.last_prompt = list(messages)
        self.chat_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.chat_scripts:
            raise AssertionError("chat script exhausted")
        return self.chat_scripts.pop(0)

    async def stream(self, messages: list[LLMMessage]):
        answer = await self.chat(messages)
        if self.chunk_size:
            for index in range(0, len(answer), self.chunk_size):
                yield answer[index:index + self.chunk_size]
                if self.delay:
                    await asyncio.sleep(self.delay)
        else:
            yield answer

    async def complete(self, messages: list[LLMMessage], tools=None) -> LLMResponse:
        self.last_prompt = list(messages)
        if self.complete_scripts:
            return self.complete_scripts.pop(0)
        return LLMResponse(content=await self.chat(messages))


class ExplodingTTS:
    """Provider whose synthesis always fails: audio degrades, text survives."""

    name = "exploding_tts"

    async def health(self) -> bool:
        return True

    async def synthesize(self, text: str, state: str):
        raise RuntimeError("synthesis engine exploded")


class FakeDesktopRegistry:
    """Minimal registry exposing one grounded fake desktop tool per scenario."""

    def __init__(self, results: dict[str, dict], *, risk: RiskLevel = RiskLevel.READ_ONLY) -> None:
        self.results = results
        self.risk = risk
        self.executed: list[tuple[str, dict]] = []

    def should_route_to_agent(self, text: str) -> bool:
        lowered = text.casefold()
        return any(token in lowered for token in ("abre", "abra", "bloco", "pinga", "status")) and "capital" not in lowered

    def llm_tools(self) -> list[dict]:
        return [{
            "type": "function",
            "function": {
                "name": "desktop_launch",
                "description": "Abre um aplicativo.",
                "parameters": {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]},
            },
        }]

    def preflight(self, name: str, payload: dict) -> dict:
        return {"risk_level": self.risk.value, "resource_key": f"desktop:{payload.get('app', '')}", "host": "local"}

    def resolve_remote_target(self, text: str) -> dict[str, str] | None:
        return None

    async def execute(self, name: str, payload: dict, *, exposure: str = "internal") -> ToolResult:
        assert exposure in {"internal", "llm", "api"}
        key = str(payload.get("app", ""))
        data = self.results.get(key) or next(iter(self.results.values()))
        self.executed.append((name, dict(payload)))
        return ToolResult(tool=name, risk=self.risk, ok=bool(data.get("success", True)), data=data, elapsed_ms=1)


_ACTIVE_ORCHESTRATORS: list[RealtimeOrchestrator] = []


@pytest.fixture(autouse=True)
async def _track_orchestrators():
    yield
    pending = list(_ACTIVE_ORCHESTRATORS)
    _ACTIVE_ORCHESTRATORS.clear()
    for orchestrator in pending:
        queue = getattr(orchestrator, "speech_queue", None)
        if queue is not None:
            await queue.stop()


async def build_orchestrator(tmp_path: Path, llm, tts=None, tools_registry=None):
    bus = EventBus()
    memory = MemoryRepository(tmp_path / "nyra.db", bus)
    await memory.initialize()
    state_machine = StateMachine(memory, bus)
    speech_queue = SpeechQueue()
    speech_queue.start()
    v4 = V4SettingsManager()
    telemetry = RealtimeTelemetry()
    avatar = AvatarController(bus)
    voice_processor = SimpleNamespace(config=None)
    perception = SimpleNamespace(snapshot=SimpleNamespace(enabled=False))
    orchestrator = RealtimeOrchestrator(
        llm, memory, state_machine, bus, tts or DisabledTTS(), speech_queue,
        settings_manager=v4, telemetry=telemetry, perception=perception,
        avatar=avatar, voice_processor=voice_processor,
    )
    if tools_registry is not None:
        orchestrator.tools = tools_registry
    _ACTIVE_ORCHESTRATORS.append(orchestrator)
    return orchestrator, bus


def tool_response(app: str) -> LLMResponse:
    return LLMResponse(tool_calls=[LLMToolCall(
        function=LLMToolFunction(name="desktop_launch", arguments={"app": app}),
        tool_call_id="call_isolation_1",
    )])


# ------------------------------------------------------------------ 101..105


@pytest.mark.asyncio
async def test_101_simple_then_simple_greeting_never_reuses_answer(tmp_path: Path):
    llm = ScriptedLLM(["A resposta é 4.", "Olá! Estou por aqui."])
    orchestrator, bus = await build_orchestrator(tmp_path, llm)

    first = await orchestrator.converse("quanto é 2+2?", synthesize=False)
    second = await orchestrator.converse("oi", synthesize=False)

    assert first.response == "A resposta é 4."
    assert second.response != first.response
    assert "4" not in second.response
    assert llm.chat_calls == 1, "saudação isolada usa resposta local determinística"
    assert first.turn_id != second.turn_id
    snapshot = orchestrator.turns.snapshot()
    assert snapshot["metrics"]["completed_turns"] >= 2
    assert snapshot["active"] == []


@pytest.mark.asyncio
async def test_102_simple_then_tool_tool_result_stays_out_of_context_echo(tmp_path: Path):
    llm = ScriptedLLM(["A capital do Brasil é Brasília."])
    registry = FakeDesktopRegistry({"bloco_de_notas": {
        "success": True, "effect_verified": True, "verification_status": "VERIFIED",
        "message": "Janela visível confirmada.", "pid": 4242,
    }})
    orchestrator, _bus = await build_orchestrator(tmp_path, llm, tools_registry=registry)

    first = await orchestrator.converse("qual a capital do Brasil?", synthesize=False)
    assert "Brasília" in first.response

    llm.complete_scripts = [
        tool_response("bloco_de_notas"),
        LLMResponse(content="Janela do bloco de notas confirmada como visível."),
    ]
    second = await orchestrator.converse("abre o bloco de notas", synthesize=False)

    assert registry.executed, "tool deve ter sido executada no turno B"
    assert "Brasília" not in second.response
    assert second.turn_id != first.turn_id


@pytest.mark.asyncio
async def test_103_tool_then_simple_greeting_has_no_tool_content(tmp_path: Path):
    registry = FakeDesktopRegistry({"bloco_de_notas": {
        "success": True, "effect_verified": True, "verification_status": "VERIFIED",
        "stdout": "Notepad pid 9988 window visible", "pid": 9988,
    }})
    llm = ScriptedLLM([])
    orchestrator, _bus = await build_orchestrator(tmp_path, llm, tools_registry=registry)

    llm.complete_scripts = [
        tool_response("bloco_de_notas"),
        LLMResponse(content="Janela do bloco de notas confirmada como visível."),
    ]
    first = await orchestrator.converse("abre o bloco de notas", synthesize=False)
    assert "bloco" in first.response.casefold()

    llm.chat_scripts.append("Olá! Como posso ajudar?")
    second = await orchestrator.converse("oi", synthesize=False)

    folded = second.response.casefold()
    for forbidden in ("notepad", "bloco", "9988", "pid"):
        assert forbidden not in folded, f"vazamento detectado: {forbidden!r}"
    # O turno B não consulta o modelo nem reapresenta o prompt operacional.
    assert llm.chat_calls == 0
    assert llm.chat_scripts == ["Olá! Como posso ajudar?"]


@pytest.mark.asyncio
async def test_104_tool_then_tool_results_are_not_shared(tmp_path: Path):
    registry = FakeDesktopRegistry({
        "ping": {"success": True, "stdout": "tempo=3ms TTL=64", "exit_code": 0},
        "status": {"success": True, "stdout": "servico RUNNING pid 77", "exit_code": 0},
    })
    llm = ScriptedLLM([])
    orchestrator, _bus = await build_orchestrator(tmp_path, llm, tools_registry=registry)

    llm.complete_scripts = [
        tool_response("ping"),
        LLMResponse(content="Gateway respondeu com tempo de 3ms."),
    ]
    turn_a = await orchestrator.converse("pinga o gateway", synthesize=False)

    llm.complete_scripts = [
        tool_response("status"),
        LLMResponse(content="Serviço aparece RUNNING no momento."),
    ]
    turn_b = await orchestrator.converse("status do serviço", synthesize=False)

    assert turn_a.turn_id != turn_b.turn_id
    assert "RUNNING" not in turn_a.response and "pid 77" not in turn_a.response
    assert "3ms" not in turn_b.response
    assert len(registry.executed) == 2


@pytest.mark.asyncio
async def test_105_failed_tool_error_does_not_leak_into_next_turn(tmp_path: Path):
    registry = FakeDesktopRegistry({"bloco_de_notas": {
        "success": False, "error_code": "EXECUTION_FAILED",
        "message": "spawn falhou ACCESS DENIED 0x80070005", "stderr": "ACCESS DENIED",
    }})
    llm = ScriptedLLM([])
    orchestrator, _bus = await build_orchestrator(tmp_path, llm, tools_registry=registry)

    llm.complete_scripts = [
        tool_response("bloco_de_notas"),
        LLMResponse(content="Não consegui abrir o aplicativo solicitado agora."),
    ]
    turn_a = await orchestrator.converse("abre o bloco de notas", synthesize=False)
    assert "ACCESS DENIED" not in turn_a.response  # grounded rewrite removes raw error

    llm.chat_scripts.append("Oi! Estou por aqui.")
    turn_b = await orchestrator.converse("oi", synthesize=False)
    folded = turn_b.response.casefold()
    for forbidden in ("access denied", "0x80070005", "bloco"):
        assert forbidden not in folded


# --------------------------------------------------------------- 106 TTS fail


@pytest.mark.asyncio
async def test_106_tts_failure_degrades_audio_and_next_turn_works(tmp_path: Path):
    llm = ScriptedLLM(["Primeira resposta completa.", "Segunda resposta saudável."])
    exploding_tts = ExplodingTTS()
    orchestrator, bus = await build_orchestrator(tmp_path, llm, tts=exploding_tts)

    first = await orchestrator.converse("turno com tts quebrado", synthesize=True)
    assert first.response == "Primeira resposta completa."
    assert first.pipeline_status == TurnStatus.AUDIO_DEGRADED.value
    types = [event.type for event in bus.history()]
    assert EventType.TTS_CHUNK_FAILED in types

    second = await orchestrator.converse("oi de novo", synthesize=True)
    assert second.response == "Segunda resposta saudável."
    assert "Primeira" not in second.response


# --------------------------------------------------- 107/108 cross-turn guard


@pytest.mark.asyncio
async def test_107_new_turn_cancels_previous_stream_and_records_cleanup(tmp_path: Path):
    slow_llm = ScriptedLLM(["A" * 4000], delay=0.02, chunk_size=8)
    orchestrator, _bus = await build_orchestrator(tmp_path, slow_llm)

    task_a = asyncio.create_task(orchestrator.converse("conte uma historia longa", synthesize=False))
    await asyncio.sleep(0.25)
    task_b = asyncio.create_task(orchestrator.converse("resposta curta", synthesize=False))
    slow_llm.chat_scripts.append("Resposta curta pronta.")

    results = await asyncio.gather(task_a, task_b, return_exceptions=True)
    outcome_a = results[0]
    turn_b = results[1]

    # Turno A: ou foi cancelado (exceção) ou não produziu o texto integral.
    if isinstance(outcome_a, object) and not isinstance(outcome_a, BaseException):
        assert len(outcome_a.response) < 4000
    assert not isinstance(turn_b, BaseException)
    assert turn_b.response == "Resposta curta pronta."
    statuses = {item["turn_id"]: item["status"] for item in orchestrator.turns.recent}
    assert len(statuses) >= 2
    assert orchestrator.turns.metrics.active_turns == 0


def test_108_cross_turn_observation_lookup_is_rejected():
    ledger_a = GroundingLedger(turn_id="turn_aaaa")
    ledger_a.record(
        tool_call_id="call_x1", tool_name="system_shell",
        result_data={"success": True, "stdout": "pid 1234"}, risk_level="READ_ONLY",
    )

    ledger_b = GroundingLedger(turn_id="turn_bbbb")

    # Ledger B não conhece observação do turno A...
    assert ledger_b.by_call_id.get("call_x1") is None
    # ...e consulta explícita com turn_id errado é rejeitada por design.
    with pytest.raises(CrossTurnObservationError) as excinfo:
        ledger_b.observation("call_x1", turn_id="turn_cccc")
    assert excinfo.value.error_code == CROSS_TURN_OBSERVATION_REJECTED

    with pytest.raises(CrossTurnObservationError):
        ledger_a.observation("call_x1", turn_id="turn_bbbb")


def test_109_grounding_ledger_is_turn_scoped_by_default():
    ledger = GroundingLedger(turn_id="turn_scope")
    observation = ledger.record(
        tool_call_id="call_s1", tool_name="system_shell",
        result_data={"success": True, "stdout": "ok"}, risk_level="READ_ONLY",
    )
    assert observation.turn_id == "turn_scope"
    fetched = ledger.observation("call_s1", turn_id="turn_scope")
    assert fetched.tool_call_id == "call_s1"


def test_110_cross_turn_rejection_increments_metric():
    registry = TurnRegistry()
    before = registry.metrics.cross_turn_rejections
    registry.record_cross_turn_rejection(turn_id="turn_z")
    assert registry.metrics.cross_turn_rejections == before + 1


# ------------------------------------------------------- 111/112 approval flow


def test_111_strict_sim_only_resumes_single_pending_approval():
    gate = ShellApprovalGate(ttl_seconds=60)
    record = gate.request(command="Stop-Service Spooler", shell="powershell",
                          working_directory="C:\\", timeout_seconds=30,
                          risk_level=RiskLevel.ELEVATED, target="local")
    granted = gate.resolve_user_statement("sim")
    assert granted is not None and granted.approval_id == record.approval_id
    assert granted.status == "GRANTED"


def test_112_random_sim_without_pending_does_not_resume_anything():
    gate = ShellApprovalGate(ttl_seconds=60)
    assert gate.resolve_user_statement("sim") is None
    assert gate.resolve_user_statement("oi") is None

    # Duas aprovações pendentes ao mesmo tempo => nenhuma decisão ambígua.
    gate.request(command="cmd-a", shell="powershell", working_directory="C:\\",
                 timeout_seconds=30, risk_level=RiskLevel.ELEVATED)
    gate.request(command="cmd-b", shell="powershell", working_directory="C:\\",
                 timeout_seconds=30, risk_level=RiskLevel.ELEVATED)
    assert gate.resolve_user_statement("sim") is None


@pytest.mark.asyncio
async def test_112b_agent_run_isolated_between_turns(tmp_path: Path, monkeypatch):
    settings = Settings(database_path=tmp_path / "agent.db", agent_enabled=True)
    controller = AgentController(settings, EventBus(), ScriptedLLM([]), FakeDesktopRegistry({}))
    await controller.initialize()

    llm = ScriptedLLM([])
    registry = FakeDesktopRegistry({"bloco_de_notas": {"success": True, "message": "janela confirmada"}})
    controller = AgentController(settings, EventBus(), llm, registry)
    await controller.initialize()

    messages = [LLMMessage(role="user", content="abre o bloco de notas")]
    llm.complete_scripts = [
        tool_response("bloco_de_notas"),
        LLMResponse(content="Janela confirmada como visível."),
    ]
    response_a = await controller.run(messages, "abrir bloco de notas", turn_id="turn_run_a")
    run_a_id = controller.last_run_for_turn("turn_run_a")

    llm.complete_scripts = [
        tool_response("bloco_de_notas"),
        LLMResponse(content="Janela confirmada novamente."),
    ]
    response_b = await controller.run(messages, "abrir bloco de notas", turn_id="turn_run_b")
    run_b_id = controller.last_run_for_turn("turn_run_b")

    assert response_a and response_b
    assert run_a_id and run_b_id and run_a_id != run_b_id
    run_b = await controller.get(run_b_id)
    assert run_b is not None and run_b.turn_id == "turn_run_b"
    assert all(step.index <= run_b.tool_calls for step in run_b.steps)
    persisted = (await controller.recent(1))[0]
    assert persisted.turn_id == "turn_run_b"


# ------------------------------------------------------- 113 concurrent turns


@pytest.mark.asyncio
async def test_113_concurrent_turns_are_isolated_and_clean(tmp_path: Path):
    llm = ScriptedLLM(["Resposta da primeira mensagem.", "Resposta da segunda mensagem."],
                      delay=0.03, chunk_size=7)
    orchestrator, _bus = await build_orchestrator(tmp_path, llm)

    results = await asyncio.gather(
        orchestrator.converse("primeira", synthesize=False),
        orchestrator.converse("segunda", synthesize=False),
        return_exceptions=True,
    )
    successes = [item for item in results if not isinstance(item, BaseException)]
    failures = [item for item in results if isinstance(item, BaseException)]
    assert successes, "pelo menos um turno deve concluir"
    contents = {getattr(item, "response", "") for item in successes}
    assert "Resposta da segunda mensagem." in contents or "Resposta da primeira mensagem." in contents
    # Nenhuma resposta mistura conteúdo das duas mensagens.
    for item in successes:
        assert not (item.response.startswith("Resposta da primeira") and "segunda" in item.response)
    # Cancelamentos também executam cleanup: nada fica ativo no registro.
    assert orchestrator.turns.snapshot()["active"] == []
    assert failures == [] or all(isinstance(item, asyncio.CancelledError) or hasattr(item, "error") for item in failures)


# ---------------------------------------------------------------- unit guards


def test_turn_context_finish_is_idempotent_and_cleans_buffers():
    turn = TurnContext("teste")
    turn.append_content("dados efêmeros")
    turn.pending_tool_calls.append({"id": "x"})
    turn.finish(TurnStatus.FAILED, error=None)
    turn.cleanup()
    assert turn.content_buffer == [] and turn.pending_tool_calls == []
    turn.finish(TurnStatus.COMPLETE, final_response="outra coisa")
    assert turn.status == TurnStatus.FAILED


def test_failed_turn_registry_marks_failure_and_clears_active():
    registry = TurnRegistry()
    turn = registry.start(TurnContext("falha"))
    registry.finish(turn.turn_id, TurnStatus.FAILED, error=None)
    assert registry.get(turn.turn_id) is None
    assert registry.metrics.failed_turns == 1
    assert registry.metrics.active_turns == 0


def test_score_match_exact_beats_partial_and_unknown_scores_zero():
    candidate = ApplicationCandidate(id="wireshark", display_name="Wireshark", source="path",
                                     launch_method="EXE", target=r"C:\Program Files\Wireshark\Wireshark.exe")
    assert score_match("Wireshark", candidate.display_name) == 1.0
    assert 0 < score_match("Wire", candidate.display_name) < 1
    assert score_match("zzzznaoexiste", candidate.display_name) == 0.0


def test_elevation_detection_helpers():
    stdout = "Stop-Service : Service 'Spooler' cannot be stopped due to access denied."
    stderr = ""
    assert is_access_denied_output(stdout, stderr) is True
    assert is_access_denied_output("tudo certo", "saida normal") is False

    from pathlib import Path as _Path

    script = build_elevated_script("Get-Service Spooler", "powershell", _Path("o.txt"), _Path("e.txt"))
    assert "Get-Service Spooler" in script and "o.txt" in script
    import inspect

    from app.tools import elevated_broker

    assert "-Verb RunAs" in inspect.getsource(elevated_broker.run_elevated)
    assert "-Verb RunAs" not in inspect.getsource(elevated_broker.build_elevated_script)


# ------------------------------------------------------------- API contract


def test_api_chat_creates_and_returns_turn_id(monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from app import main as app_main
    from app.main import app
    from app.orchestrator import ChatResult

    monkeypatch.setattr(app_main.settings, "ollama_preload", False)
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.services.llm, "ready", AsyncMock(return_value=True))
        captured: dict = {}

        async def fake_converse(text, synthesize=True, turn=None):
            captured["turn"] = turn
            return ChatResult(
                response_id=turn.response_id, turn_id=turn.turn_id,
                pipeline_status="TEXT_COMPLETE", response="ok", display_text="ok",
                speech_text="ok", state="neutral",
            )

        monkeypatch.setattr(app.state.services.orchestrator, "converse", fake_converse)
        response = client.post("/api/chat", json={"message": "oi", "synthesize": False})
        assert response.status_code == 200
        payload = response.json()
        assert payload["turn_id"].startswith("turn_")
        assert captured["turn"] is not None and captured["turn"].turn_id == payload["turn_id"]

        # turn_id fornecido pelo cliente é respeitado (voz/texto compartilham a arquitetura)
        fixed = client.post("/api/chat", json={"message": "oi", "synthesize": False, "turn_id": f"turn_{'a' * 12}"})
        assert fixed.json()["turn_id"] == f"turn_{'a' * 12}"


def test_api_chat_failure_returns_structured_error_model(monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from app import main as app_main
    from app.main import app

    monkeypatch.setattr(app_main.settings, "ollama_preload", False)
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.services.llm, "ready", AsyncMock(return_value=True))

        async def boom(text, synthesize=True, turn=None):
            raise RuntimeError("Ollama returned an empty stream")

        monkeypatch.setattr(app.state.services.orchestrator, "converse", boom)
        response = client.post("/api/chat", json={"message": "oi", "synthesize": False})
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert detail["exception_type"] == "RuntimeError"
        assert detail["error_code"] == "PIPELINE_FAILURE"
        assert detail["stage"] in {"llm", "pipeline"}
        assert detail["turn_id"].startswith("turn_")


def test_turn_metrics_endpoint_exposes_registry_snapshot(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as app_main
    from app.main import app

    monkeypatch.setattr(app_main.settings, "ollama_preload", False)
    with TestClient(app) as client:
        snapshot = client.get("/api/turns/metrics").json()
        assert {"metrics", "active", "recent"} <= set(snapshot)
        assert {"active_turns", "completed_turns", "failed_turns",
                "cross_turn_rejections", "late_events_dropped"} <= set(snapshot["metrics"])
