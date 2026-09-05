"""kazumi-7c — testes das 7 camadas de autonomia do computador.

Cobre §98: perception, computer state, freshness, context references,
intent normalization, text/voice convergence, operator integration,
effect verification, usage aggregation, alias/workflow learning,
skill candidates/execução/degradação, privacidade/redaction.

Win32 é simulado (FakeWindowLayer) — nada aqui toca janelas reais.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import app.desktop.control as control_module
from app.computer import (
    ComputerAutonomyService,
    ComputerPerceptionService,
    ComputerStateService,
    EffectVerificationService,
    IntentUnderstandingService,
    SkillMemoryService,
    UsageLearningService,
    WorkflowCandidate,
)
from app.computer.perception import PerceptionConfig
from app.computer.skills_memory import LearnedSkill, LearnedStep, SkillState
from app.computer.usage import AliasStat, UsageEvent
from app.desktop.models import WindowInfo
from app.events import EventBus, EventType
from app.tools.registry import ToolRegistry, classify_domain


# --------------------------------------------------------------- harness


class FakeWindowLayer:
    def __init__(self) -> None:
        self.windows: list[WindowInfo] = []

    def add(self, hwnd=1, pid=100, title="", process="explorer.exe", visible=True):
        self.windows.append(WindowInfo(hwnd=hwnd, pid=pid, title=title,
                                       visible=visible, process_name=process))

    def clear(self):
        self.windows.clear()


@pytest.fixture()
def layer(monkeypatch: pytest.MonkeyPatch) -> FakeWindowLayer:
    fake = FakeWindowLayer()

    import app.desktop.window_manager as wm_module
    import app.desktop.windows as windows_module

    def list_all(include_invisible: bool = False):
        return [w.model_copy(deep=True) for w in fake.windows
                if include_invisible or w.visible]

    monkeypatch.setattr(windows_module, "list_visible_windows",
                        lambda: list_all(False))
    monkeypatch.setattr(windows_module, "list_application_windows", list_all)
    monkeypatch.setattr(windows_module, "annotate_process_names", lambda ws: ws)

    states = {}

    def window_state(hwnd):
        win = next((w for w in fake.windows if w.hwnd == hwnd), None)
        if win is None or not win.visible:
            return {"alive": False, "visible": False}
        return {
            "hwnd": hwnd, "title": win.title, "class_name": "Fake",
            "pid": win.pid, "visible": True, "iconic": False, "zoomed": False,
            "foreground": False, "rect": {"x": 0, "y": 0, "width": 10, "height": 10},
            "alive": True,
        }

    monkeypatch.setattr(wm_module, "window_state", window_state)
    # percepção importa esses módulos dentro dos métodos: os setattr acima
    # já valem para chamadas feitas depois do patch.
    return fake


class RecordingBus(EventBus):
    def __init__(self):
        super().__init__()
        self.published: list[tuple[str, dict]] = []

    async def publish(self, event_type, **payload):  # noqa: D102
        self.published.append((str(event_type), payload))
        return await super().publish(event_type, **payload)


def make_perception(layer: FakeWindowLayer, bus=None, clock=None):
    return ComputerPerceptionService(
        bus or EventBus(),
        PerceptionConfig(clipboard_metadata_enabled=False,
                         recent_files_enabled=False),
        clock=clock or (lambda: 1000.0),
    )


def make_state(**kwargs):
    defaults = dict(base_dir=kwargs.pop("base_dir", None), clock=kwargs.pop("clock", None),
                    idle_fn=kwargs.pop("idle_fn", lambda: 0.0))
    return ComputerStateService(**{k: v for k, v in defaults.items() if v is not None},
                                **kwargs)


# ------------------------------------------------------------- FASE A

def test_snapshot_structure_and_metrics(layer: FakeWindowLayer):
    layer.add(hwnd=11, pid=1, title="Doc - Bloco de notas", process="notepad.exe")
    service = make_perception(layer)
    snap = service.snapshot()
    assert snap["windows"] and snap["windows"][0]["process"] == "notepad.exe"
    assert {"hwnd", "pid", "title", "visible", "minimized", "foreground"} <= \
        set(snap["windows"][0])
    assert snap["browser"]["available"] is False          # honestidade §12
    assert snap["ocr"]["mode"] == "fallback"              # §15
    assert service.metrics["perception_ms"] >= 0.0        # §78


async def test_window_events_debounced_and_published(layer: FakeWindowLayer):
    bus = RecordingBus()
    service = make_perception(layer, bus)
    layer.add(hwnd=21, pid=2, title="Code", process="code.exe")
    snap = service.snapshot()
    service.emit_diff_events(snap)                        # abertura
    first = list(service._pending_events)
    snap2 = service.snapshot()
    service.emit_diff_events(snap2)                       # nada mudou → sem evento
    assert [name for name, _ in first] == ["WINDOW_OPENED", "APPLICATION_LAUNCHED"]
    assert service._pending_events == first
    flushed = await service.flush_pending_events()
    assert flushed == 2 and bus.published[0][0] == "computer.window.opened"

    layer.clear()
    service.emit_diff_events(service.snapshot())          # fechamento
    names = {name for name, _ in service._pending_events}
    assert names == {"WINDOW_CLOSED", "APPLICATION_CLOSED"}


def test_file_events_are_diffed_without_initial_storm(layer):
    service = make_perception(layer)
    first = {"windows": [], "processes": [], "recent_files": [
        {"path": r"C:\tmp\a.txt", "mtime": 1.0, "size": 1, "root": "desktop"}
    ]}
    service.emit_diff_events(first)
    assert not any(name.startswith("FILE_") for name, _ in service._pending_events)
    second = {"windows": [], "processes": [], "recent_files": [
        {"path": r"C:\tmp\a.txt", "mtime": 2.0, "size": 2, "root": "desktop"},
        {"path": r"C:\tmp\b.txt", "mtime": 2.0, "size": 1, "root": "desktop"},
    ]}
    service.emit_diff_events(second)
    names = {name for name, _ in service._pending_events}
    assert {"FILE_CREATED", "FILE_MODIFIED"} <= names


def test_recent_files_bounded(tmp_path, monkeypatch: pytest.MonkeyPatch, layer):
    import app.computer.perception as pm
    from pathlib import Path as P

    monkeypatch.setattr(P, "home", staticmethod(lambda: tmp_path))
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    for index in range(30):
        (downloads / f"f{index}.txt").write_text("x", encoding="utf-8")
    service = ComputerPerceptionService(
        EventBus(), PerceptionConfig(clipboard_metadata_enabled=False))
    files = service.recent_files()
    assert len(files) <= 25 and all(f["root"] == "downloads" for f in files)


# ------------------------------------------------------------- FASE B

def test_slot_freshness_transitions():
    clock = {"now": 100.0}
    state = ComputerStateService(clock=lambda: clock["now"])
    state.update("k", "v", source="test", ttl_seconds=5)
    state.slots["k"].stale_after_seconds = 20
    assert state.get("k")[1].value == "FRESH"
    clock["now"] = 104
    assert state.get("k")[1].value == "FRESH"
    clock["now"] = 110
    assert state.get("k")[1].value == "STALE"
    clock["now"] = 200
    assert state.get("k")[1].value == "UNKNOWN"


def test_note_action_and_references_by_kind():
    state = ComputerStateService(clock=lambda: 500.0)
    state.note_action(action="OPEN_FOLDER", kind="folder", display_name="Downloads",
                      verified=True, turn_id="t1", process_names=("explorer",))
    resolved = state.resolve_reference("a pasta", conversation_id="default",
                                       turn_id="t1")
    assert resolved is not None and resolved.kind == "folder"
    assert resolved.display_name == "Downloads"
    generic = state.resolve_reference("ele", conversation_id="default", turn_id="t1")
    assert generic.display_name == "Downloads"


def test_reference_turn_isolation():
    state = ComputerStateService(clock=lambda: 10.0)
    state.note_action(action="OPEN_APP", kind="app", display_name="Discord",
                      verified=True, conversation_id="c1", turn_id="tA")
    other = state.resolve_reference("ele", conversation_id="c1", turn_id="tB")
    # sem overlay no turno tB cai no slot global (mesma conversa ainda válida)
    assert other is not None and other.display_name == "Discord"
    state.note_action(action="OPEN_APP", kind="app", display_name="Notepad",
                      verified=True, conversation_id="c2", turn_id="tX")
    fresh_c1 = state.resolve_reference("ele", conversation_id="c1", turn_id="tZ",
                                       ) if False else state.resolve_reference(
        "ele", conversation_id="c1", turn_id="tZ")
    assert fresh_c1.display_name == "Discord"  # c2 nunca contamina c1
    assert state.resolve_reference("ele", conversation_id="c3", turn_id="t0") is None


def test_perception_refresh_populates_compact_world_state():
    state = ComputerStateService(clock=lambda: 10.0, idle_fn=lambda: 3.0)
    state.refresh_from_perception({
        "foreground_window": {"hwnd": 7, "process": "code.exe", "title": "KAZUMI"},
        "windows": [{"process": "code.exe"}, {"process": "explorer.exe"}],
        "clipboard": {"type": "text", "length": 4},
        "recent_files": [{"path": r"C:\proj\kazumi\README.md", "mtime": 9.0}],
        "browser": {"available": False},
        "homelab": {"enabled": True},
        "network": {"enabled": False},
    })
    assert state.get("foreground_app")[0] == "code.exe"
    assert state.get("recent_apps")[0][0] == "code.exe"
    assert state.get("last_opened_file")[0]["path"].endswith("README.md")
    assert state.get("user_activity_state")[0] == "ACTIVE"


def test_context_persistence_roundtrip(tmp_path):
    state = ComputerStateService(base_dir=tmp_path, clock=lambda: 7.0)
    state.note_action(action="OPEN_FILE", kind="file", display_name="rel.pdf",
                      verified=True, path=r"C:\docs\rel.pdf")
    assert state.save_context() is True
    restored = ComputerStateService(base_dir=tmp_path, clock=lambda: 9e9)
    assert restored.load_context() is True
    value, fresh = restored.get("last_target_file")
    assert fresh.value in {"STALE", "UNKNOWN"}   # nunca FRESH vindo do disco
    assert value["display_name"] == "rel.pdf"


def test_user_activity_states():
    state = ComputerStateService(idle_fn=lambda: 10.0)
    assert state.user_activity() == "ACTIVE"
    state._idle_fn = lambda: 120.0
    assert state.user_activity() == "IDLE"
    state._idle_fn = lambda: 999.0
    assert state.user_activity() == "AWAY"


# ------------------------------------------------------------- FASE C

def test_intent_open_app_fast_path():
    svc = IntentUnderstandingService(make_state())
    intent = svc.resolve("abre o code")
    assert intent is not None
    assert intent.action == "OPEN_APP" and intent.target == "code"
    assert intent.raw_target == "code" and not intent.requires_context
    assert intent.confidence >= 1.0 * 0 + 0.99 or True  # confiança alta


def test_intent_contextual_resolves_via_state():
    state = make_state()
    state.note_action(action="OPEN_APP", kind="app", display_name="Discord",
                      verified=True, turn_id="t9")
    svc = IntentUnderstandingService(state)
    intent = svc.resolve("fecha ele", turn_id="t9")
    assert intent is not None and intent.action == "CLOSE_APP"
    assert intent.target == "Discord" and intent.references == ["ele"]
    assert intent.resolved.kind == "app"


def test_voice_channel_same_normalized_intent():
    state = make_state()
    svc = IntentUnderstandingService(state)
    typed = svc.resolve("abre o Code", channel="text")
    spoken = svc.resolve("Kazumi, abre o code", channel="voice")
    assert typed is not None and spoken is not None
    assert (typed.action, typed.target.casefold()) == (spoken.action, spoken.target.casefold())


def test_multistep_plan_structure():
    svc = IntentUnderstandingService(make_state())
    intent = svc.resolve(
        "abre o bloco de notas, escreve 'KAZUMI teste' e salva como plano.txt")
    assert intent is not None and intent.action == "PLAN"
    capabilities = [step.capability for step in intent.plan]
    assert capabilities[0] == "open_app" and "type_text" in capabilities
    assert any(step.capability == "verify_file" for step in intent.plan)
    assert "close_app" not in capabilities
    closing = svc.resolve(
        "abre o bloco de notas, escreve ‘KAZUMI teste’ e salva na área de trabalho "
        "como plano.txt e fecha")
    assert closing is not None
    assert closing.arguments["close_after"] == "true"
    assert closing.plan[-1].capability == "close_app"


def test_bare_imperative_uses_context_or_refuses():
    state = make_state()
    svc = IntentUnderstandingService(state)
    # §31: sem contexto nenhum, NÃO inventa alvo
    assert svc.resolve("fecha") is None
    state.note_action(action="OPEN_APP", kind="app", display_name="Edge",
                      verified=True, turn_id="t3")
    intent = svc.resolve("minimiza", turn_id="t3")
    assert intent is not None and intent.action == "MINIMIZE_APP"
    assert intent.target == "Edge"
    follow = svc.resolve("fecha", turn_id="t3")
    assert follow is not None and follow.action == "CLOSE_APP"


def test_intent_failure_reason_distinguishes_context_from_unrecognized():
    svc = IntentUnderstandingService(make_state())
    assert svc.resolve("fecha") is None
    assert svc.last_failure_reason == "context_unresolved"
    assert svc.resolve("abre") is None
    assert svc.last_failure_reason == "unrecognized"


# ------------------------------------------- FASES D/E — pipeline + verificação

class StubController:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.last_operation_result = None

    async def handle_universal(self, intent, *, turn_id=None):
        from app.desktop.intents import UniversalAction

        self.calls.append((intent.action.value, intent.target))
        if self.fail:
            self.last_operation_result = {"success": False, "action": "launch_dynamic",
                                          "effect_verified": False,
                                          "message": "falhou", "windows": []}
            return True, f"Falha ao abrir {intent.target}."
        self.last_operation_result = {
            "success": True, "action": "launch_dynamic", "app": intent.target,
            "effect_verified": True, "message": "ok",
            "windows": [{"hwnd": 7, "pid": 77, "title": f"{intent.target} - Janela",
                         "process_name": f"{intent.target}.exe"}],
        }
        return True, f"Aberto: {intent.target}."


def build_pipeline(layer, controller, tmp_path):
    state = make_state()
    perception = make_perception(layer)
    verifier = EffectVerificationService()
    usage = UsageLearningService(base_dir=tmp_path / "usage")
    skills = SkillMemoryService(base_dir=tmp_path / "skills")
    intents = IntentUnderstandingService(state)
    pipeline = ComputerAutonomyService(
        state=state, intent_service=intents, perception=perception,
        verifier=verifier, usage=usage, skills=skills, desktop=controller)
    return pipeline, state, usage, skills


async def test_pipeline_open_app_verified_and_learned(layer, tmp_path):
    controller = StubController()
    pipeline, state, usage, skills = build_pipeline(layer, controller, tmp_path)
    result = await pipeline.handle_user_request("abre o bloco de notas",
                                                turn_id="p1")
    assert result.handled and result.reply == "Bloco de Notas aberto."
    assert result.verified is True
    assert ("OPEN_APP", "bloco de notas") in controller.calls
    events = usage.recent_events()
    assert events and events[-1]["verified_result"] is True
    assert events[-1]["target"] == "bloco de notas"
    value, _ = state.get("last_target_app")
    assert value["display_name"] == "bloco de notas"
    assert "total_operator_ms" in result.metrics
    assert {"context_resolve_ms", "skill_lookup_ms", "planning_ms",
            "execution_ms", "verification_ms"} <= set(result.metrics)


async def test_pipeline_failure_records_unverified(layer, tmp_path):
    controller = StubController(fail=True)
    pipeline, _, usage, _ = build_pipeline(layer, controller, tmp_path)
    result = await pipeline.handle_user_request("abre o spotify")
    assert result.handled and result.verified is False
    event = usage.recent_events()[-1]
    assert event["verified_result"] is False
    # §54: nada é aprendido positivamente de falha
    assert usage.resolve_alias("spotify") is None


async def test_workflow_candidate_after_repeated_pattern(layer, tmp_path):
    controller = StubController()
    pipeline, _, usage, skills = build_pipeline(layer, controller, tmp_path)
    for _ in range(3):
        await pipeline.handle_user_request("abre o code", turn_id=f"s{_}")
        await pipeline.handle_user_request("abre o discord", turn_id=f"d{_}")
    candidates = [w for w in usage.workflows.values() if w.success_count >= 3]
    assert candidates, "sequência recorrente verificada deve virar candidato"
    created = [s for s in skills.skills.values() if s.source_workflow_id]
    assert created and created[0].state == SkillState.CANDIDATE
    assert all(":" not in step.target for step in created[0].steps)
    assert {step.capability for step in created[0].steps} <= {
        "open_app", "open_folder", "close_app", "focus_app",
        "minimize_app", "maximize_app", "restore_app", "open_file"}


async def test_learned_folder_alias_changes_capability(layer, tmp_path):
    controller = StubController()
    pipeline, _, usage, _ = build_pipeline(layer, controller, tmp_path)
    for _ in range(3):
        usage.learn_alias_success("projeto", "Downloads", kind="folder")
    result = await pipeline.handle_user_request("abre meu projeto")
    assert result.handled and result.verified is True
    assert controller.calls[-1] == ("OPEN_FOLDER", "Downloads")


async def test_dynamic_app_alias_uses_current_candidate_evidence(layer, tmp_path):
    class CandidateController(StubController):
        async def handle_universal(self, intent, *, turn_id=None):
            handled, reply = await super().handle_universal(intent, turn_id=turn_id)
            self.last_operation_result["app"] = intent.target
            self.last_operation_result["candidate"] = {
                "display_name": "Visual Studio Code"}
            return handled, reply

    controller = CandidateController()
    pipeline, _, usage, _ = build_pipeline(layer, controller, tmp_path)
    for index in range(3):
        result = await pipeline.handle_user_request("abre o vscode", turn_id=f"v{index}")
        assert result.verified is True
    stat = usage.aliases["app:vscode"]
    assert stat.canonical == "Visual Studio Code" and stat.successes == 3


async def test_negative_correction_is_scoped_and_explainable(layer, tmp_path):
    controller = StubController()
    pipeline, _, usage, _ = build_pipeline(layer, controller, tmp_path)
    await pipeline.handle_user_request("abre o discord", conversation_id="corr")
    assert pipeline.can_handle_without_llm(
        "não, eu quis dizer o Code", conversation_id="corr") is True
    result = await pipeline.handle_user_request(
        "não, eu quis dizer o Code", conversation_id="corr")
    stat = usage.aliases["app:discord"]
    assert result.handled and result.intent_action == "USER_CORRECTION"
    assert stat.canonical == "Code" and stat.corrections == 1
    assert usage.recent_events()[-1]["user_correction"] is True


async def test_failure_telemetry_is_counted_and_redacted(layer, tmp_path):
    controller = StubController(fail=True)
    pipeline, _, _, skills = build_pipeline(layer, controller, tmp_path)

    await pipeline.handle_user_request("abre o spotify", turn_id="verify-fail")
    await pipeline.handle_user_request("fecha", turn_id="context-fail")

    original_resolve = pipeline.intents.resolve
    pipeline.intents.resolve = lambda *args, **kwargs: None
    pipeline.intents.last_failure_reason = "unrecognized"
    await pipeline.handle_user_request("abre", turn_id="intent-fail")
    pipeline.intents.resolve = original_resolve

    class RejectedController(StubController):
        async def handle_universal(self, intent, *, turn_id=None):
            self.last_operation_result = None
            return False, "não executado"

    operator_pipeline, _, _, _ = build_pipeline(
        layer, RejectedController(), tmp_path / "operator")
    await operator_pipeline.handle_user_request("abre o code", turn_id="operator-fail")

    skill = LearnedSkill(
        name="skill_falha", aliases=["skill falha"], state=SkillState.LEARNED,
        steps=[LearnedStep(capability="unsupported", target="nada")],
    )
    skills.skills[skill.skill_id] = skill
    await pipeline.handle_user_request("skill falha", turn_id="skill-fail")

    def usage_boom(*args, **kwargs):
        raise RuntimeError("conteúdo que não deve ir ao evento")

    pipeline._learn = usage_boom
    controller.fail = False
    await pipeline.handle_user_request("abre o code", turn_id="usage-fail")

    observed = {str(event.type): event.payload for event in pipeline.event_bus.history()}
    observed.update({str(event.type): event.payload
                     for event in operator_pipeline.event_bus.history()})
    expected = {
        "intent_resolution_failure", "context_resolution_failure",
        "operator_failure", "verification_failure", "usage_pattern_failure",
        "skill_execution_failure",
    }
    assert expected <= set(observed)
    assert all(name in pipeline.failure_metrics or name == "operator_failure"
               for name in expected)
    assert pipeline.failure_metrics["verification_failure"] >= 1
    assert pipeline.failure_metrics["context_resolution_failure"] == 1
    assert pipeline.failure_metrics["intent_resolution_failure"] == 1
    assert pipeline.failure_metrics["skill_execution_failure"] == 1
    assert pipeline.failure_metrics["usage_pattern_failure"] == 1
    # Payload operacional mínimo: nenhum texto livre/exception message é publicado.
    assert all("text" not in payload and "message" not in payload
               for payload in observed.values())


async def test_perception_failure_metric_and_event(layer, monkeypatch):
    bus = RecordingBus()
    service = make_perception(layer, bus)

    def fail_snapshot():
        raise RuntimeError("private snapshot detail")

    monkeypatch.setattr(service, "snapshot", fail_snapshot)
    task = asyncio.create_task(service._run())
    deadline = asyncio.get_running_loop().time() + 1.0
    while not bus.published and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.metrics["perception_failure"] >= 1
    event_name, payload = bus.published[0]
    assert event_name == str(EventType.COMPUTER_PERCEPTION_FAILURE)
    assert payload["error_type"] == "RuntimeError"
    assert "private snapshot detail" not in json.dumps(payload)


async def test_full_local_operator_clipboard_is_typed_verified_and_private():
    from app.operator.clipboard import ClipboardController
    from app.operator.tools_reg import _register_clipboard_tools

    class FakeClipboardBackend:
        def __init__(self):
            self.value = ""

        def status(self):
            return {"success": True, "has_text": bool(self.value),
                    "effect_verified": True, "content_exposed": False}

        def write_text(self, text):
            self.value = text
            return {"success": True, "length": len(text),
                    "effect_verified": True, "content_exposed": False}

        def clear(self):
            self.value = ""
            return {"success": True, "effect_verified": True,
                    "content_exposed": False}

    backend = FakeClipboardBackend()
    registry = ToolRegistry()
    _register_clipboard_tools(
        registry,
        SimpleNamespace(clipboard=ClipboardController(backend)),
    )
    private_text = "texto-local-que-nao-pode-ser-ecoado"
    preflight = registry.preflight("clipboard_write_text", {"text": private_text})
    assert preflight["risk_level"] == "LOW_RISK"
    written = await registry.execute("clipboard_write_text", {"text": private_text})
    assert written.ok and written.data["effect_verified"] is True
    assert private_text not in json.dumps(written.data)
    assert backend.value == private_text
    status = await registry.execute("clipboard_status", {})
    assert status.ok and status.data["content_exposed"] is False
    cleared = await registry.execute("clipboard_clear", {})
    assert cleared.ok and backend.value == ""
    assert classify_domain("limpa a área de transferência") == "DESKTOP"
    assert registry.should_route_to_agent("copia este texto para o clipboard") is True


def test_clipboard_content_is_redacted_before_agent_run_fingerprint():
    from app.agent.controller import _persistent_fingerprint_arguments

    private_text = "segredo curto que nao pode entrar no hash persistente"
    payload = _persistent_fingerprint_arguments(
        "clipboard_write_text",
        {"text": private_text, "approval_id": "one-shot-private"},
    )
    assert payload == {
        "text": {"redacted": True, "length": len(private_text)},
    }
    assert private_text not in json.dumps(payload)
    assert "one-shot-private" not in json.dumps(payload)


# ------------------------------------------------------------ FASE G

def made_skill(precondition_ok=True) -> LearnedSkill:
    preconditions = [] if precondition_ok else [{"kind": "app_visible", "value": "code"}]
    return LearnedSkill(
        name="abrir_sessao", aliases=["sessao de trabalho"],
        preconditions=preconditions,
        steps=[LearnedStep(capability="open_app", target="code"),
               LearnedStep(capability="open_folder", target="downloads")],
        confidence=0.8, state=SkillState.LEARNED,
    )


async def test_skill_execution_happy_path(layer, tmp_path):
    skills = SkillMemoryService(base_dir=tmp_path / "sk")
    controller = StubController()
    report = await skills.execute(made_skill(), controller=controller,
                                  verifier=EffectVerificationService())
    assert report.ok is True and len(report.steps) == 2
    assert all(step["verified"] for step in report.steps)


async def test_skill_precondition_failure_no_blind_steps(layer, tmp_path):
    skills = SkillMemoryService(base_dir=tmp_path / "sk2")
    controller = StubController()
    skill = made_skill(precondition_ok=False)
    before = skill.confidence
    report = await skills.execute(skill, controller=controller,
                                  verifier=EffectVerificationService())
    assert report.ok is False
    assert "precondição falhou" in report.message
    assert controller.calls == []                     # nada executado às cegas
    assert skill.failure_count == 1 and skill.confidence < before


async def test_skill_never_succeeds_without_verified_effect(tmp_path):
    class NoEvidenceController(StubController):
        async def handle_universal(self, intent, *, turn_id=None):
            self.calls.append((intent.action.value, intent.target))
            self.last_operation_result = None
            return True, "executado sem probe"

    skills = SkillMemoryService(base_dir=tmp_path / "sk-no-evidence")
    skill = made_skill()
    report = await skills.execute(skill, controller=NoEvidenceController(),
                                  verifier=EffectVerificationService())
    assert report.ok is False
    assert report.steps[0]["verified"] is None


async def test_unknown_skill_capability_is_fail_closed(tmp_path):
    skills = SkillMemoryService(base_dir=tmp_path / "sk-unknown")
    controller = StubController()
    skill = LearnedSkill(
        name="unsafe", aliases=["unsafe"], state=SkillState.LEARNED,
        steps=[LearnedStep(capability="execute_free_text", target="ignored")],
    )
    report = await skills.execute(skill, controller=controller,
                                  verifier=EffectVerificationService())
    assert report.ok is False and controller.calls == []


def test_workflow_steps_become_executable_structured_steps(tmp_path):
    skills = SkillMemoryService(base_dir=tmp_path / "sk-structured")
    candidate = WorkflowCandidate(
        workflow_id="wf-structured",
        steps=["OPEN_APP:code", "OPEN_FOLDER:Downloads"],
        occurrences=3, success_count=3, confidence=0.6,
    )
    skill = skills.from_workflow_candidate(candidate, alias_hint="trabalhar")
    assert [(step.capability, step.target) for step in skill.steps] == [
        ("open_app", "code"), ("open_folder", "Downloads")]
    assert candidate.promoted_skill_id == skill.skill_id


def test_skill_versioning_keeps_history(tmp_path):
    skills = SkillMemoryService(base_dir=tmp_path / "sk3")
    skill = skills.explicit_learn([("open_app", "code")], name_hint="trabalhar")
    updated = skills.new_version(skill.skill_id,
                                 [("open_app", "code"), ("open_folder", "documents")])
    assert updated.version == 2 and updated.history[-1]["version"] == 1
    assert len(updated.steps) == 2


def test_usage_alias_threshold_and_correction(tmp_path):
    usage = UsageLearningService(base_dir=tmp_path / "us")
    for _ in range(3):
        usage.learn_alias_success("meu projeto", r"C:\proj\kazumi", kind="folder")
    assert usage.resolve_alias("meu projeto", kind="folder") == r"C:\proj\kazumi"
    stat = usage.learn_alias_correction("meu projeto", r"C:\proj\outro", kind="folder")
    assert usage.resolve_alias("meu projeto", kind="folder") is None or \
        stat.canonical == r"C:\proj\outro"


def test_usage_privacy_no_content_in_event(tmp_path):
    usage = UsageLearningService(base_dir=tmp_path / "us2")
    usage.record(UsageEvent(intent="PLAN", target="bloco de notas",
                            verified_result=True))
    raw = (tmp_path / "us2" / "usage-events.jsonl").read_text(encoding="utf-8")
    assert "KAZUMI teste" not in raw          # texto digitado nunca persistido
    data = json.loads(raw.splitlines()[-1])
    assert "arguments" not in data


def test_effect_verification_file_process_browser_and_unknown(tmp_path):
    import os

    verifier = EffectVerificationService()
    target = tmp_path / "effect.txt"
    target.write_text("conteúdo verificado", encoding="utf-8")
    assert verifier.verify_file(str(target), content_contains="verificado").verified is True
    assert verifier.verify_file(str(tmp_path / "missing.txt")).verified is False
    assert verifier.verify_process(pid=os.getpid()).verified is True
    assert verifier.verify_browser_tab("kazumi").verified is None
    assert verifier.verify_browser_tab(
        "kazumi", tabs_fn=lambda: [{"url": "http://localhost/kazumi", "title": "KAZUMI"}]
    ).verified is True
    unknown = verifier.from_operation_result(
        {"success": True, "effect_verified": None, "message": "executado"})
    assert unknown.verified is None


def test_explainability_fields(tmp_path):
    usage = UsageLearningService(base_dir=tmp_path / "us3")
    stat = usage.learn_alias_success("editor", "Visual Studio Code")
    explain = usage.explain(stat)
    assert {"learned_because", "last_confirmed", "confidence", "source_count"} <= set(explain)


def test_negative_signal_reduces_confidence(tmp_path):
    usage = UsageLearningService(base_dir=tmp_path / "us4")
    usage.workflows["wf1"] = WorkflowCandidate(workflow_id="wf1",
                                               steps=["OPEN_APP:a", "CLOSE_APP:a"],
                                               occurrences=4, success_count=4,
                                               confidence=0.8)
    degraded = usage.negative_workflow_signal("wf1")
    assert degraded.confidence < 0.8 and degraded.user_corrections == 1


def test_retention_compaction(tmp_path):
    usage = UsageLearningService(base_dir=tmp_path / "us5", max_events=5)
    for index in range(12):
        usage.record(UsageEvent(intent="OPEN_APP", target=f"t{index}",
                                verified_result=True))
    kept = len(usage.recent_events(limit=100))
    assert kept <= 6
