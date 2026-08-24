from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from pydantic import ValidationError

from app.attention import AttentionEngine
from app.avatar import AvatarController
from app.character import StateMachine
from app.events import Event, EventBus, EventType
from app.llm.base import LLMMessage, LLMProvider
from app.memory import MemoryRepository
from app.perception.context import ContextSelector
from app.perception.models import ForegroundApp, PerceptionSnapshot, SystemSnapshot
from app.perception import PCAwareness
from app.proactive import ProactiveEngine
from app.reactions import ReactionEngine
from app.realtime.cooldowns import CooldownManager
from app.realtime.models import PrivacyConfig, RealtimeConfig
from app.realtime.orchestrator import RealtimeOrchestrator
from app.realtime.sentence_assembler import SentenceAssembler
from app.realtime.settings import V4SettingsManager
from app.realtime.telemetry import RealtimeTelemetry
from app.skills.models import SkillDefinition, SkillPermission
from app.skills.registry import SkillRegistry
from app.speech.queue import SpeechQueue
from app.speech.tts import TTSProvider
from app.speech.voice_processor import VoiceProcessor, VoiceProcessorConfig


class StreamingMock(LLMProvider):
    def __init__(self) -> None:
        self.finished = asyncio.Event()

    @property
    def name(self) -> str:
        return "streaming_mock"

    async def health(self) -> bool:
        return True

    async def chat(self, messages: list[LLMMessage]) -> str:
        return "Primeira sentença realmente completa. Segunda sentença completa."

    async def stream(self, messages: list[LLMMessage]):
        yield "Primeira sentença realmente completa. "
        await asyncio.sleep(.08)
        yield "Segunda sentença completa."
        self.finished.set()


class RecordingTTS(TTSProvider):
    def __init__(self, root: Path, llm: StreamingMock | None = None) -> None:
        self.root, self.llm, self.calls = root, llm, []
        self.started_before_llm_finished = False

    @property
    def name(self) -> str:
        return "recording_tts"

    async def health(self) -> bool:
        return True

    async def synthesize(self, text: str, state: str = "neutral", options=None) -> Path:
        self.calls.append(text)
        if self.llm:
            self.started_before_llm_finished = self.started_before_llm_finished or not self.llm.finished.is_set()
        output = self.root / f"sample-{len(self.calls)}.wav"
        sf.write(output, np.zeros(800, dtype=np.float32), 16000, subtype="PCM_16")
        return output


def test_sentence_assembler_preserves_technical_periods_and_minimum_chunks():
    assembler = SentenceAssembler(minimum_characters=12, minimum_words=2)
    assert assembler.feed("O host 192.168.1.1 usa a versão v4.2.1. ") == ["O host 192.168.1.1 usa a versão v4.2.1."]
    assert assembler.feed("Tudo ") == []
    assert assembler.feed("normal! Próximo") == ["Tudo normal!"]
    assert assembler.finish() == ["Próximo"]


def test_privacy_rejects_screen_capture():
    with pytest.raises(ValidationError):
        PrivacyConfig(screen_capture=True)


def test_context_selector_only_includes_relevant_fields():
    snapshot = PerceptionSnapshot(enabled=True, foreground_app=ForegroundApp(process="Code.exe", classification="VS Code"), system=SystemSnapshot(cpu_percent=25, ram_percent=40))
    selector = ContextSelector()
    assert selector.select("qual é o aplicativo atual?", snapshot).find("VS Code") >= 0
    assert selector.select("conte uma piada", snapshot) == ""
    assert "mouse" not in selector.select("como está a CPU?", snapshot)


def test_cooldown_and_skill_permissions():
    async def run():
        bus, cooldowns = EventBus(), CooldownManager()
        registry = SkillRegistry(bus, cooldowns)
        async def ok(payload): return {"value": payload.get("value", 1)}
        registry.register(SkillDefinition(name="read_status", description="read", cooldown_seconds=30), ok)
        registry.register(SkillDefinition(name="open_safe_app", description="confirm", permission=SkillPermission.CONFIRM_REQUIRED), ok)
        registry.register(SkillDefinition(name="dangerous_action", description="danger", permission=SkillPermission.DANGEROUS), ok)
        assert (await registry.execute("read_status", {"value": 2})).data["value"] == 2
        with pytest.raises(RuntimeError): await registry.execute("read_status")
        with pytest.raises(PermissionError): await registry.execute("open_safe_app")
        assert (await registry.execute("open_safe_app", confirmed=True)).ok
        with pytest.raises(PermissionError): await registry.execute("dangerous_action", confirmed=True)
    asyncio.run(run())


def test_attention_decay_and_critical_reaction_are_deterministic():
    async def run():
        bus = EventBus(); attention = AttentionEngine(bus); await attention.start()
        realtime = RealtimeConfig(proactive_reactions=True)
        perception = PCAwareness(bus, realtime, PrivacyConfig())
        avatar = AvatarController(bus); proactive = ProactiveEngine(CooldownManager())
        reactions = ReactionEngine(bus, avatar, perception, proactive, realtime)
        await reactions.start()
        await bus.publish(EventType.SENTINEL_ALERT, severity="critical")
        assert attention.current.source == "neutral" or attention.current.priority <= 90
        assert reactions.last_reaction["reaction"] == "SENTINEL_CRITICAL"
        assert avatar.state.expression == "concerned"
        await reactions.stop(); await attention.stop()
    asyncio.run(run())


def test_voice_processor_preserves_duration_and_avoids_clipping(tmp_path: Path):
    rate = 24000
    source = tmp_path / "raw.wav"
    tone = (.25 * np.sin(2 * np.pi * 220 * np.arange(rate) / rate)).astype(np.float32)
    sf.write(source, tone, rate, subtype="PCM_16")
    processor = VoiceProcessor(VoiceProcessorConfig(enabled=True, signature_effect=.03))
    destination = tmp_path / "processed.wav"
    processor._process_sync(source, destination, "focused")
    raw, processed = processor.analyze(source), processor.analyze(destination)
    assert abs(raw["duration_ms"] - processed["duration_ms"]) <= 1
    assert not processed["clipping"] and processed["peak"] <= .9851


def test_streaming_starts_tts_before_llm_finishes_and_keeps_order(tmp_path: Path):
    async def run():
        bus = EventBus(); memory = MemoryRepository(tmp_path / "memory.db", bus); await memory.initialize()
        llm = StreamingMock(); tts = RecordingTTS(tmp_path, llm); speech = SpeechQueue(); speech.start()
        settings = V4SettingsManager(tmp_path / "v4.json")
        settings.value.voice_processor.enabled = False
        telemetry = RealtimeTelemetry(); perception = PCAwareness(bus, settings.value.realtime, settings.value.privacy)
        avatar = AvatarController(bus); processor = VoiceProcessor(settings.value.voice_processor)
        orchestrator = RealtimeOrchestrator(
            llm, memory, StateMachine(memory, bus), bus, tts, speech,
            settings_manager=settings, telemetry=telemetry, perception=perception,
            avatar=avatar, voice_processor=processor,
        )
        result = await orchestrator.converse("teste", synthesize=True)
        assert tts.started_before_llm_finished
        assert tts.calls == ["Primeira sentença realmente completa.", "Segunda sentença completa."]
        assert len(result.audio_urls) == 2
        assert result.timing["end_to_first_audio_ms"] < result.timing["response_complete_ms"]
        await speech.stop()
    asyncio.run(run())


def test_interrupt_cancels_active_stream_and_clears_queue(tmp_path: Path):
    class SlowLLM(StreamingMock):
        async def stream(self, messages):
            yield "Uma sentença inicial completa. "
            await asyncio.sleep(10)

    async def run():
        bus = EventBus(); events = []
        async def collect(event: Event): events.append(event.type)
        await bus.subscribe(collect)
        memory = MemoryRepository(tmp_path / "interrupt.db", bus); await memory.initialize()
        llm = SlowLLM(); tts = RecordingTTS(tmp_path); speech = SpeechQueue(); speech.start()
        settings = V4SettingsManager(tmp_path / "v4-interrupt.json"); settings.value.voice_processor.enabled = False
        perception = PCAwareness(bus, settings.value.realtime, settings.value.privacy)
        orchestrator = RealtimeOrchestrator(
            llm, memory, StateMachine(memory, bus), bus, tts, speech,
            settings_manager=settings, telemetry=RealtimeTelemetry(), perception=perception,
            avatar=AvatarController(bus), voice_processor=VoiceProcessor(settings.value.voice_processor),
        )
        task = asyncio.create_task(orchestrator.converse("interrompa", synthesize=True))
        await asyncio.sleep(.05)
        assert await orchestrator.interrupt("test")
        with pytest.raises(asyncio.CancelledError): await task
        assert speech.pending == 0
        assert EventType.SPEECH_CANCELLED in events
        await speech.stop()
    asyncio.run(run())
