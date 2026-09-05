from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from app.events import Event, EventBus, EventType
from app.emotional_presence import VoicePresentationAdapter
from app.intelligence.storage import IntelligenceStore
from app.proactive_presence import (
    ProactiveDecision,
    ProactiveMode,
    ProactivePresenceService,
    ProactiveSettingsUpdate,
)
from app.speech.tts import TtsCapabilities


class Clock:
    def __init__(self) -> None:
        self.value = time.time()

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeWorldState:
    def __init__(self, *, user: str = "IDLE", assistant: str = "IDLE") -> None:
        self.user = user
        self.assistant = assistant
        self.active_goal = None
        self.most_relevant = None

    def get_snapshot(self) -> dict:
        return {
            "user_activity_state": {"value": self.user},
            "assistant_state": {"value": self.assistant},
            "active_goal": {"value": self.active_goal},
            "most_relevant_open_loop": {"value": self.most_relevant},
        }


class FakeOpenLoops:
    def __init__(self, loops=()) -> None:
        self.loops = list(loops)

    async def get(self, loop_id: str):
        return next((item for item in self.loops if item.id == loop_id), None)

    async def list(self, limit: int = 300):
        return self.loops[:limit]


class ReadyProvider:
    name = "fake-local"
    active_provider = "fake-local"
    active_voice = "KAZUMI_FIXED_VOICE"
    default_voice = "KAZUMI_FIXED_VOICE"

    async def health(self) -> bool:
        return True

    def capabilities(self):
        return TtsCapabilities(supports_native_speed=True)


class SharedEmotionalPresence:
    def __init__(self, provider) -> None:
        self.provider = provider
        self.calls = 0

    def build_voice_style(self, **_context):
        self.calls += 1
        return VoicePresentationAdapter().build_voice_style("concerned", .43, {}, self.provider)


class FakeSpeechQueue:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[str] = []

    async def synthesize(self, _provider, text: str, _state: str, _priority, **_options):
        self.calls.append(text)
        return self.path


def runtime_settings(**overrides):
    values = dict(
        proactive_presence_enabled=True,
        proactive_presence_mode="NORMAL",
        proactive_voice_enabled=False,
        proactive_presence_cooldown_seconds=120,
        proactive_presence_max_per_hour=20,
        proactive_presence_defer_ttl_seconds=600,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


async def build_service(tmp_path: Path, *, world=None, loops=(), settings=None,
                        clock=None, voice=False, emotional=False):
    store = IntelligenceStore(tmp_path / "kazumi.db")
    await store.initialize()
    bus = EventBus(history_size=500)
    provider = ReadyProvider()
    speech = FakeSpeechQueue(tmp_path / "proactive.wav")
    emotional_presence = SharedEmotionalPresence(provider) if emotional else None
    service = ProactivePresenceService(
        settings=settings or runtime_settings(proactive_voice_enabled=voice),
        event_bus=bus,
        intelligence_store=store,
        world_state=world or FakeWorldState(),
        open_loops=FakeOpenLoops(loops),
        speech_queue=speech,
        provider_getter=lambda: provider,
        emotional_presence=emotional_presence,
        clock=clock or Clock(),
    )
    return service, bus, speech, store


def event(event_type: EventType, **payload) -> Event:
    return Event(type=event_type, payload=payload)


@pytest.mark.asyncio
async def test_low_event_is_ignored_and_idle_alone_never_starts_conversation(tmp_path: Path):
    service, _bus, _speech, _store = await build_service(tmp_path)

    decision = await service.evaluate_event(event(EventType.NETWORK_STATUS_UPDATED, latency_ms=21))
    idle = await service.evaluate_event(event(EventType.USER_IDLE, level="IDLE"))

    assert decision and decision.decision == ProactiveDecision.IGNORE
    assert idle is None
    assert await service.store.notifications() == []


@pytest.mark.asyncio
async def test_routine_network_snapshots_use_zero_io_fast_path(tmp_path: Path):
    service, bus, _speech, _store = await build_service(tmp_path)
    await service.start()

    for value in range(20):
        await bus.publish(EventType.NETWORK_STATUS_UPDATED, latency_ms=value)
    await asyncio.sleep(.05)
    status = await service.status()
    await service.stop()

    assert status["counters"]["fast_ignored"] == 20
    assert status["counters"]["decisions_audited"] == 0
    assert status["storage"]["decisions"] == 0
    assert status["queue"]["size"] == 0


@pytest.mark.asyncio
async def test_relevant_monitor_event_notifies_without_user_message(tmp_path: Path):
    service, bus, _speech, _store = await build_service(tmp_path)
    await service.start()
    await bus.publish(
        EventType.MONITOR_JOB_COMPLETED,
        monitor_id="mon_vm120", objective="a VM 120 voltar online",
        completion_reason="CONDITION_MET", voice=False,
    )
    deadline = asyncio.get_running_loop().time() + 2
    while not await service.store.notifications():
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(.01)
    decision = (await service.store.recent_decisions(limit=1))[0]
    await service.stop()

    assert decision and decision.decision == ProactiveDecision.CHAT_MESSAGE
    notice = (await service.store.notifications())[0]
    assert notice.message == "A condição aguardada para a VM 120 voltar online foi atendida."
    assert notice.execution_authorized is False and notice.action_budget_consumed == 0
    assert any(item.type == EventType.PROACTIVE_PRESENCE_NOTIFICATION for item in bus.history())
    assert not any(item.type in {EventType.USER_TEXT_RECEIVED, EventType.USER_SPEECH_RECEIVED}
                   for item in bus.history())


@pytest.mark.asyncio
async def test_critical_bypasses_activity_and_do_not_disturb_without_voice_overlap(tmp_path: Path):
    world = FakeWorldState(user="IDLE", assistant="IDLE")
    settings = runtime_settings(proactive_voice_enabled=False)
    service, _bus, speech, _store = await build_service(
        tmp_path, world=world, settings=settings, voice=True,
    )

    warning = await service.evaluate_event(event(
        EventType.RUNTIME_FAILED, service_id="voice-worker",
    ))
    service.settings.mode = ProactiveMode.DO_NOT_DISTURB
    service.settings.voice_enabled = True
    world.user = "ACTIVE"
    world.assistant = "SPEAKING"
    decision = await service.evaluate_event(event(
        EventType.RUNTIME_CRASH_LOOP, service_id="voice-worker",
    ))

    assert warning and warning.decision == ProactiveDecision.CHAT_MESSAGE
    assert decision and decision.priority.value == "CRITICAL"
    assert decision.decision == ProactiveDecision.CHAT_MESSAGE
    assert len(await service.store.notifications()) == 2
    assert speech.calls == []  # Voice task never overlaps an active speaker.


@pytest.mark.asyncio
async def test_cooldown_and_semantic_dedup_coalesce_repeats(tmp_path: Path):
    clock = Clock()
    service, _bus, _speech, _store = await build_service(tmp_path, clock=clock)
    first = await service.evaluate_event(event(
        EventType.NETWORK_INTERNET_DOWN, message="A internet caiu.",
    ))
    repeated = await service.evaluate_event(event(
        EventType.NETWORK_INTERNET_DOWN, message="Sem conexão com a internet.",
    ))

    assert first and first.decision == ProactiveDecision.CHAT_MESSAGE
    assert repeated and repeated.decision == ProactiveDecision.LOG_ONLY
    assert repeated.reason == "semantic_cooldown_coalesced"
    assert len(await service.store.notifications()) == 1
    assert service.counters["duplicates_coalesced"] == 1
    assert service.counters["spam_events"] == 0


@pytest.mark.asyncio
async def test_user_active_gets_ui_while_idle_gets_persistent_chat(tmp_path: Path):
    active = FakeWorldState(user="ACTIVE")
    service, _bus, _speech, _store = await build_service(tmp_path / "active", world=active)
    ui = await service.evaluate_event(event(
        EventType.MONITOR_JOB_CHANGED, monitor_id="mon_a", objective="o download",
    ))

    idle = FakeWorldState(user="IDLE")
    other, _bus2, _speech2, _store2 = await build_service(tmp_path / "idle", world=idle)
    chat = await other.evaluate_event(event(
        EventType.MONITOR_JOB_CHANGED, monitor_id="mon_b", objective="o download",
    ))

    assert ui and ui.decision == ProactiveDecision.UI_NOTIFICATION
    assert chat and chat.decision == ProactiveDecision.CHAT_MESSAGE


@pytest.mark.asyncio
async def test_assistant_speaking_defers_then_flushes_when_idle(tmp_path: Path):
    world = FakeWorldState(user="IDLE", assistant="SPEAKING")
    service, _bus, _speech, _store = await build_service(tmp_path, world=world)
    deferred = await service.evaluate_event(event(
        EventType.TASK_FINISHED, task_id="task_pdf", state="SUCCEEDED",
        objective="a indexação dos PDFs",
    ))
    assert deferred and deferred.decision == ProactiveDecision.DEFER
    assert (await service.store.counts())["deferred"] == 1

    world.assistant = "IDLE"
    assert await service.flush_deferred() == 1
    assert (await service.store.counts())["deferred"] == 0
    assert len(await service.store.notifications()) == 1


@pytest.mark.asyncio
async def test_hourly_budget_defer_has_persistent_retry_backoff(tmp_path: Path):
    clock = Clock()
    settings = runtime_settings(
        proactive_presence_max_per_hour=1,
        proactive_presence_defer_ttl_seconds=7200,
    )
    service, _bus, _speech, _store = await build_service(
        tmp_path, settings=settings, clock=clock,
    )
    first = await service.evaluate_event(event(
        EventType.TASK_FINISHED, task_id="task_one", state="SUCCEEDED",
        objective="a primeira tarefa",
    ))
    clock.advance(181)
    second = await service.evaluate_event(event(
        EventType.TASK_FINISHED, task_id="task_two", state="SUCCEEDED",
        objective="a segunda tarefa",
    ))

    assert first and first.decision == ProactiveDecision.CHAT_MESSAGE
    assert second and second.decision == ProactiveDecision.DEFER
    deferred = await service.store.deferred()
    assert deferred[0].metadata["retry_not_before"] > clock()
    assert await service.flush_deferred() == 0
    assert (await service.store.counts())["decisions"] == 2

    clock.advance(3601)
    assert await service.flush_deferred() == 1
    assert len(await service.store.notifications()) == 2


@pytest.mark.asyncio
async def test_quiet_mode_allows_only_high_and_critical(tmp_path: Path):
    settings = runtime_settings(proactive_presence_mode="QUIET")
    service, _bus, _speech, _store = await build_service(tmp_path, settings=settings)
    normal = await service.evaluate_event(event(
        EventType.MONITOR_JOB_COMPLETED, monitor_id="mon_normal",
        objective="o download", completion_reason="CONDITION_MET",
    ))
    high = await service.evaluate_event(event(
        EventType.USB_DEVICE_UNKNOWN,
        device={"device_id": "usb-new", "friendly_name": "Unidade externa"},
    ))

    assert normal and normal.decision == ProactiveDecision.LOG_ONLY
    assert high and high.decision == ProactiveDecision.CHAT_MESSAGE


@pytest.mark.asyncio
async def test_monitor_completion_uses_actionable_open_loop_context(tmp_path: Path):
    loop = SimpleNamespace(
        id="loop_vm", title="continuar a configuração da VM 120", goal="goal_vm",
        state="ACTIVE", waiting_for={"kind": "monitor_condition"},
        related_monitor=["mon_vm"], related_task=[], related_artifact=[],
    )
    world = FakeWorldState(user="IDLE")
    world.most_relevant = {"id": "loop_vm"}
    service, _bus, _speech, _store = await build_service(tmp_path, world=world, loops=[loop])

    decision = await service.evaluate_event(event(
        EventType.MONITOR_JOB_COMPLETED, monitor_id="mon_vm",
        objective="a VM 120 voltar", completion_reason="CONDITION_MET",
    ))
    notice = (await service.store.notifications())[0]

    assert decision and decision.open_loop_id == "loop_vm"
    assert "Posso continuar a configuração da VM 120" in notice.message


@pytest.mark.asyncio
async def test_monitor_relation_waits_for_open_loop_consumer_without_order_coupling(tmp_path: Path):
    loop = SimpleNamespace(
        id="loop_delayed", title="retomar a configuração", goal="goal_delayed",
        state="RESOLVED", waiting_for={"kind": "monitor_condition"},
        related_monitor=["mon_delayed"], related_task=[], related_artifact=[],
    )

    class DelayedOpenLoops(FakeOpenLoops):
        def __init__(self):
            super().__init__([loop])
            self.calls = 0

        async def list(self, limit: int = 300):
            self.calls += 1
            return [] if self.calls == 1 else self.loops

    service, _bus, _speech, _store = await build_service(tmp_path)
    delayed = DelayedOpenLoops()
    service.open_loops = delayed
    decision = await service.evaluate_event(event(
        EventType.MONITOR_JOB_COMPLETED, monitor_id="mon_delayed",
        objective="a configuração", completion_reason="CONDITION_MET",
    ))

    assert delayed.calls == 2
    assert decision and decision.open_loop_id == "loop_delayed"
    assert decision.goal_id == "goal_delayed"


@pytest.mark.asyncio
async def test_task_completion_and_failure_are_natural_and_distinct(tmp_path: Path):
    done, _bus, _speech, _store = await build_service(tmp_path / "done")
    completed = await done.evaluate_event(event(
        EventType.TASK_STATE_CHANGED, task_id="task_ok", state="SUCCEEDED",
        objective="a indexação dos PDFs",
    ))
    failed_service, _bus2, _speech2, _store2 = await build_service(tmp_path / "failed")
    failed = await failed_service.evaluate_event(event(
        EventType.TASK_STATE_CHANGED, task_id="task_bad", state="FAILED",
        objective="a indexação dos PDFs", reason="um arquivo não pôde ser lido",
    ))

    assert completed and completed.decision == ProactiveDecision.CHAT_MESSAGE
    assert (await done.store.notifications())[0].message == "Terminei a indexação dos PDFs."
    assert failed and failed.priority.value == "HIGH"
    assert "arquivo não pôde ser lido" in (await failed_service.store.notifications())[0].message


@pytest.mark.asyncio
async def test_open_loop_actionable_is_presented_but_plain_lifecycle_is_log_only(tmp_path: Path):
    loop = SimpleNamespace(
        id="loop_cfg", title="A configuração da VM", goal="goal_cfg", state="ACTIVE",
        waiting_for={"kind": "monitor_condition"}, related_monitor=["mon_cfg"],
        related_task=[], related_artifact=[],
    )
    service, _bus, _speech, _store = await build_service(tmp_path, loops=[loop])
    actionable = await service.evaluate_event(event(
        EventType.OPEN_LOOP_STATE_CHANGED, loop_id="loop_cfg", state="ACTIVE",
    ))
    resolved = await service.evaluate_event(event(
        EventType.OPEN_LOOP_RESOLVED, loop_id="loop_cfg", state="RESOLVED",
    ))

    assert actionable and actionable.decision == ProactiveDecision.CHAT_MESSAGE
    assert resolved and resolved.decision == ProactiveDecision.LOG_ONLY


@pytest.mark.asyncio
async def test_usb_known_is_ignored_and_unknown_is_notified(tmp_path: Path):
    known, _bus, _speech, _store = await build_service(tmp_path / "known")
    ignored = await known.evaluate_event(event(
        EventType.USB_DEVICE_KNOWN_CONNECTED,
        device={"device_id": "mouse-1", "friendly_name": "Mouse conhecido"},
    ))
    unknown, _bus2, _speech2, _store2 = await build_service(tmp_path / "unknown")
    warned = await unknown.evaluate_event(event(
        EventType.USB_DEVICE_UNKNOWN,
        device={"device_id": "storage-9", "friendly_name": "Kingston"},
    ))

    assert ignored and ignored.decision == ProactiveDecision.IGNORE
    assert warned and warned.decision == ProactiveDecision.CHAT_MESSAGE
    assert "Kingston" in (await unknown.store.notifications())[0].message


@pytest.mark.asyncio
async def test_network_recovery_requires_prior_relevant_outage(tmp_path: Path):
    clock = Clock()
    service, _bus, _speech, _store = await build_service(tmp_path, clock=clock)
    orphan = await service.evaluate_event(event(EventType.NETWORK_INTERNET_RECOVERED))
    assert orphan and orphan.decision == ProactiveDecision.LOG_ONLY

    clock.advance(121)
    down = await service.evaluate_event(event(EventType.NETWORK_INTERNET_DOWN))
    recovered = await service.evaluate_event(event(EventType.NETWORK_INTERNET_RECOVERED))

    assert down and down.decision == ProactiveDecision.CHAT_MESSAGE
    assert recovered and recovered.decision == ProactiveDecision.CHAT_MESSAGE
    assert len(await service.store.notifications()) == 2


@pytest.mark.asyncio
async def test_restart_restores_notification_and_cooldown(tmp_path: Path):
    clock = Clock()
    first, _bus, _speech, store = await build_service(tmp_path, clock=clock)
    sent = await first.evaluate_event(event(
        EventType.USB_DEVICE_UNKNOWN,
        device={"device_id": "usb-persist", "friendly_name": "USB persistente"},
    ))
    assert sent and sent.decision == ProactiveDecision.CHAT_MESSAGE

    restarted = ProactivePresenceService(
        settings=runtime_settings(), event_bus=EventBus(), intelligence_store=store,
        world_state=FakeWorldState(), open_loops=FakeOpenLoops(), clock=clock,
    )
    repeated = await restarted.evaluate_event(event(
        EventType.USB_DEVICE_UNKNOWN,
        device={"device_id": "usb-persist", "friendly_name": "USB persistente"},
    ))

    assert repeated and repeated.decision == ProactiveDecision.LOG_ONLY
    assert len(await restarted.store.notifications()) == 1
    assert (await restarted.store.counts())["decisions"] == 2


@pytest.mark.asyncio
async def test_voice_is_optional_and_uses_local_tts_when_ready(tmp_path: Path):
    service, bus, speech, _store = await build_service(tmp_path, voice=True, emotional=True)
    decision = await service.evaluate_event(event(
        EventType.USB_DEVICE_UNKNOWN,
        device={"device_id": "usb-voice", "friendly_name": "USB novo"},
    ))
    assert decision and decision.decision == ProactiveDecision.VOICE_AND_CHAT
    if service._voice_tasks:
        await asyncio.gather(*service._voice_tasks)

    assert speech.calls == ["Detectei um dispositivo USB desconhecido: USB novo."]
    assert service.emotional_presence.calls == 1
    assert any(item.type == EventType.TTS_STARTED
               and item.payload.get("voice_style", {}).get("emotion") == "concerned"
               for item in bus.history())
    assert any(item.type == EventType.TTS_FINISHED
               and item.payload.get("source") == "proactive_presence" for item in bus.history())


@pytest.mark.asyncio
async def test_settings_change_is_persisted_and_refreshable(tmp_path: Path, monkeypatch):
    settings_path = tmp_path / "settings-v33.json"
    monkeypatch.setattr("app.proactive_presence.service.save_runtime_settings",
                        lambda updates: settings_path.write_text(str(updates), encoding="utf-8"))
    service, _bus, _speech, _store = await build_service(tmp_path, settings=runtime_settings())

    value = await service.update(ProactiveSettingsUpdate(
        enabled=False, mode=ProactiveMode.QUIET, voice_enabled=True,
    ))

    assert value.enabled is False and value.mode == ProactiveMode.QUIET
    assert value.voice_enabled is True and settings_path.is_file()


@pytest.mark.asyncio
async def test_artifact_selfdev_and_operator_sources_are_audited(tmp_path: Path):
    artifact = SimpleNamespace(artifact_id="art-1", path="E:/kazumi/report.pdf")
    loop = SimpleNamespace(
        id="loop_report", title="Gerar relatório", goal="goal_report", state="ACTIVE",
        waiting_for=None, related_monitor=[], related_task=[], related_artifact=[artifact],
    )
    service, _bus, _speech, _store = await build_service(tmp_path, loops=[loop])
    ready = await service.evaluate_event(event(
        EventType.ARTIFACT_CONTEXT_UPDATED,
        artifact={"artifact_id": "art-1", "path": "E:/kazumi/report.pdf"}, verified=True,
    ))
    service.clock.advance(181)
    selfdev = await service.evaluate_event(event(
        EventType.SELFDEV_VALIDATION_PASS, issue_id="SELFDEV-TEST",
    ))
    service.clock.advance(181)
    operator = await service.evaluate_event(event(
        EventType.COMPUTER_VERIFICATION_FAILURE, operation="Abrir relatório",
    ))

    assert ready and ready.decision == ProactiveDecision.CHAT_MESSAGE
    assert selfdev and selfdev.decision == ProactiveDecision.CHAT_MESSAGE
    assert operator and operator.decision == ProactiveDecision.CHAT_MESSAGE
    assert (await service.store.counts())["decisions"] == 3
