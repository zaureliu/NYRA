from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import asyncio

import httpx
import pytest

from app.conversation.engine import ConversationEngine
from app.conversation.models import AudioSettingsUpdate, ConversationState, InterruptionTarget
from app.core.turn import PipelineFailure
from app.events import EventBus, EventType
from app.listening.models import UtteranceDecision
from app.llm.warm_manager import OllamaReadiness, OllamaWarmManager
from app.realtime.telemetry import RealtimeTelemetry
from app.speech.stt import FasterWhisperSTT
from app.tools.registry import ToolRegistry


class FakeTranscription:
    def __init__(self, text: str) -> None:
        self.text = text

    def model_dump(self, mode: str = "json") -> dict:
        return {"text": self.text, "language": "pt"}


class FakeSTT:
    name = "fake_stt"
    model_name = "tiny"
    loaded = True

    def __init__(self, text: str = "bom dia") -> None:
        self.text = text
        self.calls = 0

    async def transcribe(self, _path: Path) -> FakeTranscription:
        self.calls += 1
        return FakeTranscription(self.text)


class FakeListening:
    def __init__(self) -> None:
        self.enabled = False
        self.processing = False
        self.last_update = None

    def config(self):
        return SimpleNamespace(model_copy=lambda update: SimpleNamespace(**update))

    async def update(self, value):
        self.last_update = value
        self.enabled = bool(value.enabled)
        return {"enabled": self.enabled}

    def status(self):
        return {"enabled": self.enabled}

    def can_process(self, _client_id: str):
        return True, "ready"

    def decide(self, text: str):
        return UtteranceDecision(accepted=True, reason="hands_free", text=text, hands_free_active=True)


class FakeChat:
    def __init__(self, pipeline_status: str = "COMPLETE", audio_urls: list[str] | None = None) -> None:
        self.pipeline_status = pipeline_status
        self.audio_urls = ["/api/audio/nyra-test.wav"] if audio_urls is None else audio_urls
        self.audio_url = None

    def model_dump(self, mode: str = "json") -> dict:
        return {
            "response_id": "turn",
            "response": "Oi.",
            "pipeline_status": self.pipeline_status,
            "audio_urls": self.audio_urls,
        }


class FakeOrchestrator:
    def __init__(self) -> None:
        self.tts = SimpleNamespace(
            name="edge_tts", primary_name="edge_tts", fallback_name="kokoro",
            engine_id="edge_neural", active_engine="edge_neural",
            active_voice="en-US-AvaMultilingualNeural",
            fallback_active=False, fallback_reason=None,
            capabilities=lambda: SimpleNamespace(supports_emotion=False),
            describe=lambda: {"engine": "edge_tts"},
        )
        self.emotion_planner = SimpleNamespace(configure=lambda **_values: None)
        self.calls = []
        self.speech_cancels = 0
        self.agent = SimpleNamespace(cancel_active=self.cancel_task)

    async def converse(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return FakeChat()

    async def cancel_speech(self, _reason):
        self.speech_cancels += 1
        return True

    async def cancel_task(self, _reason):
        return True


def conversation_settings():
    return SimpleNamespace(
        conversation_engine=True,
        voice_barge_in=True,
        microphone="default",
        speaker="default",
        tts_voice="en-US-AvaMultilingualNeural",
        tts_speaking_rate=.97,
        audio_volume=.9,
        listening_mode="hands_free",
        always_listening_enabled=False,
        tts_provider="edge_tts",
        voice_emotion_mode="automatic",
        voice_expressiveness="normal",
    )


@pytest.mark.asyncio
async def test_conversation_states_empty_stt_and_direct_turn_do_not_guess(tmp_path: Path):
    bus, telemetry = EventBus(), RealtimeTelemetry()
    events = []
    await bus.subscribe(lambda event: _collect(events, event))
    stt, orchestrator = FakeSTT(""), FakeOrchestrator()
    engine = ConversationEngine(conversation_settings(), bus, stt, FakeListening(), orchestrator, telemetry)
    await engine.start()
    empty = await engine.direct_audio_turn(tmp_path / "empty.wav")
    assert empty["accepted"] is False and empty["reason"] == "empty_transcription"
    assert not orchestrator.calls
    assert engine.state == ConversationState.LISTENING
    assert EventType.STT_STARTED in events and EventType.STT_COMPLETED in events

    stt.text = "Nyra, bom dia"
    accepted = await engine.direct_audio_turn(tmp_path / "speech.wav")
    assert accepted["accepted"] is True
    assert orchestrator.calls[0][0] == "Nyra, bom dia"
    assert orchestrator.calls[0][1]["response_id"].startswith("turn_")
    assert orchestrator.calls[0][1]["turn"].approval_capable is False
    await engine.stop()


@pytest.mark.asyncio
async def test_always_listening_reports_each_voice_stage_without_hiding_text(tmp_path: Path):
    engine = ConversationEngine(
        conversation_settings(), EventBus(), FakeSTT("Nyra, fala oi"),
        FakeListening(), FakeOrchestrator(), RealtimeTelemetry(),
    )
    result = await engine.listening_audio_turn(tmp_path / "speech.wav", "client_12345678")
    assert result["accepted"] is True
    assert result["voice_stages"] == {
        "STT": "COMPLETED",
        "DECISION": "COMPLETED",
        "LLM": "COMPLETED",
        "TTS": "COMPLETED",
        "PLAYBACK": "PENDING",
    }


@pytest.mark.asyncio
async def test_always_listening_keeps_text_valid_when_tts_is_degraded(tmp_path: Path):
    orchestrator = FakeOrchestrator()

    async def degraded(text, **kwargs):
        orchestrator.calls.append((text, kwargs))
        return FakeChat("AUDIO_DEGRADED", [])

    orchestrator.converse = degraded  # type: ignore[method-assign]
    engine = ConversationEngine(
        conversation_settings(), EventBus(), FakeSTT("Nyra, responda"),
        FakeListening(), orchestrator, RealtimeTelemetry(),
    )
    result = await engine.listening_audio_turn(tmp_path / "speech.wav", "client_12345678")
    assert result["accepted"] is True
    assert result["chat"]["response"] == "Oi."
    assert result["voice_stages"]["TTS"] == "FAILED"
    assert result["voice_stages"]["PLAYBACK"] == "NOT_AVAILABLE"


@pytest.mark.asyncio
async def test_always_listening_wraps_stt_failure_with_stage_and_safe_state(tmp_path: Path):
    class MissingAudioSTT(FakeSTT):
        async def transcribe(self, path: Path):
            raise FileNotFoundError(str(path))

    engine = ConversationEngine(
        conversation_settings(), EventBus(), MissingAudioSTT(),
        FakeListening(), FakeOrchestrator(), RealtimeTelemetry(),
    )
    with pytest.raises(PipelineFailure) as raised:
        await engine.listening_audio_turn(tmp_path / "missing.wav", "client_12345678")
    assert raised.value.error.stage == "STT"
    assert raised.value.error.exception_type == "FileNotFoundError"
    assert engine.status()["last_error"] == "STT:FileNotFoundError"


async def _collect(target: list, event) -> None:
    target.append(event.type)


@pytest.mark.asyncio
async def test_barge_in_stops_speech_but_task_cancel_is_explicit():
    orchestrator = FakeOrchestrator()
    engine = ConversationEngine(
        conversation_settings(), EventBus(), FakeSTT(), FakeListening(), orchestrator, RealtimeTelemetry()
    )
    engine.state = ConversationState.SPEAKING
    assert await engine.speech_started("vad") is True
    assert orchestrator.speech_cancels == 1
    speech = await engine.interrupt(InterruptionTarget.SPEECH)
    assert speech == {"speech_cancelled": True, "task_cancelled": False, "target": "speech"}
    task = await engine.interrupt(InterruptionTarget.TASK)
    assert task["speech_cancelled"] and task["task_cancelled"]


@pytest.mark.asyncio
async def test_visible_audio_settings_reach_runtime_and_persistence(monkeypatch):
    persisted = []
    monkeypatch.setattr("app.conversation.engine.save_runtime_settings", lambda updates: persisted.append(updates))
    listening, switched = FakeListening(), []
    engine = ConversationEngine(
        conversation_settings(), EventBus(), FakeSTT(), listening, FakeOrchestrator(), RealtimeTelemetry()
    )

    async def switch(provider, voice, rate):
        switched.append((provider, voice, rate))
        return {"primary": provider}

    engine.bind_tts_switcher(switch)
    value = AudioSettingsUpdate(
        microphone="usb-mic", speaker="usb-speaker", voice="en-US-AvaMultilingualNeural", speech_speed=1.08,
        volume=.6, conversation_mode="wake_word", always_listening=True, allow_interruption=False,
    )
    result = await engine.update_audio_settings(value)
    assert result["settings"] == value.model_dump()
    assert listening.last_update.microphone == "usb-mic"
    assert listening.last_update.mode.value == "wake_word"
    assert switched == [("edge_tts", "en-US-AvaMultilingualNeural", 1.08)]
    assert persisted[0]["speaker"] == "usb-speaker" and persisted[0]["audio_volume"] == .6


def warm_settings(**overrides):
    values = {
        "llm_model": "qwen3:8b", "ollama_url": "http://ollama", "ollama_keep_alive": "-1",
        "ollama_preload": True, "ollama_warmup": True, "ollama_context_size": 8192,
        "ollama_preload_timeout_seconds": 30, "ollama_recovery_interval_seconds": 2,
        "ollama_unload_previous_model": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeBrain:
    def __init__(self, model="qwen3:8b", resident=True, ready_sequence=None):
        self.model = model
        self.resident = resident
        self.ready_sequence = list(ready_sequence or [])

    async def ready(self):
        if self.ready_sequence:
            return self.ready_sequence.pop(0)
        return self.resident


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {"load_duration": 2_000_000, "total_duration": 3_000_000}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("failed", request=httpx.Request("POST", "http://ollama"), response=httpx.Response(self.status_code))

    def json(self):
        return self._body


class FakeClient:
    payloads = []
    responses = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, json):
        self.payloads.append((url, json))
        return self.responses.pop(0) if self.responses else FakeResponse()


@pytest.mark.asyncio
async def test_ollama_preload_is_isolated_kept_alive_and_observable(monkeypatch):
    FakeClient.payloads, FakeClient.responses = [], [FakeResponse(), FakeResponse({} if False else 200)]
    monkeypatch.setattr("app.llm.warm_manager.httpx.AsyncClient", FakeClient)
    bus, events = EventBus(), []
    await bus.subscribe(lambda event: _collect(events, event))
    manager = OllamaWarmManager(warm_settings(), FakeBrain(ready_sequence=[False, True]), bus)
    result = await manager.preload(force=True)
    assert result["state"] == OllamaReadiness.OLLAMA_READY
    preload_entry, warmup_entry = FakeClient.payloads
    preload, warmup = preload_entry[1], warmup_entry[1]
    assert preload == {"model": "qwen3:8b", "stream": False, "keep_alive": -1}
    assert preload_entry[0].endswith("/api/generate")
    assert warmup_entry[0].endswith("/api/chat")
    assert warmup["messages"][0]["role"] == "system"
    assert warmup["messages"][1] == {"role": "user", "content": "Responda apenas OK."}
    assert "tools" not in warmup
    assert warmup["options"]["num_predict"] == 1
    assert EventType.OLLAMA_READINESS_CHANGED in events
    assert result["metrics"]["load_duration_ms"] == 2.0


@pytest.mark.asyncio
async def test_ollama_failure_recovery_and_model_change(monkeypatch):
    FakeClient.payloads = []
    FakeClient.responses = [FakeResponse(500), FakeResponse(), FakeResponse()]
    monkeypatch.setattr("app.llm.warm_manager.httpx.AsyncClient", FakeClient)
    brain = FakeBrain()
    manager = OllamaWarmManager(warm_settings(), brain, EventBus())
    failed = await manager.preload(force=True)
    assert failed["state"] == OllamaReadiness.OLLAMA_ERROR
    recovered = await manager.preload(force=True)
    assert recovered["state"] == OllamaReadiness.OLLAMA_READY

    unloaded = []
    async def capture_unload(model): unloaded.append(model)
    monkeypatch.setattr(manager, "_unload", capture_unload)
    brain.model = "qwen3.5:9b"
    changed = await manager.preload(force=True)
    assert changed["state"] == OllamaReadiness.OLLAMA_READY and changed["model"] == "qwen3.5:9b"
    assert unloaded == ["qwen3:8b"]


@pytest.mark.asyncio
async def test_warm_monitor_waits_before_recovery_poll_and_stops_cleanly(monkeypatch):
    manager = OllamaWarmManager(
        warm_settings(ollama_recovery_interval_seconds=30), FakeBrain(), EventBus()
    )
    calls = []

    async def preload(*, force=False):
        calls.append(force)
        manager.state = OllamaReadiness.OLLAMA_READY
        manager.model = "qwen3:8b"
        return manager.status()

    monkeypatch.setattr(manager, "preload", preload)
    manager.start()
    await asyncio.sleep(.02)
    assert calls == [False]
    await manager.stop()


@pytest.mark.asyncio
async def test_concurrent_preloads_are_serialized(monkeypatch):
    manager = OllamaWarmManager(warm_settings(), FakeBrain(), EventBus())
    active = 0
    maximum_active = 0

    async def controlled_preload(*, force=False):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(.02)
        active -= 1
        return {"force": force}

    monkeypatch.setattr(manager, "_preload", controlled_preload)
    results = await asyncio.gather(
        manager.preload(force=True),
        manager.preload(force=False),
    )
    assert maximum_active == 1
    assert results == [{"force": True}, {"force": False}]


def test_stt_instance_is_reusable_and_low_latency_defaults_are_real():
    stt = FasterWhisperSTT(beam_size=1, cpu_threads=3, workers=2)
    assert stt.loaded is False
    marker = object()
    stt._model = marker
    assert stt.loaded is True and stt._model is marker
    assert stt.beam_size == 1 and stt.cpu_threads == 3 and stt.workers == 2


def test_tool_schemas_are_contextual_for_casual_and_operational_turns():
    tools = ToolRegistry()
    assert tools.should_route_to_agent("Nyra, bom dia") is False
    assert tools.should_route_to_agent("me explica o que é DNS") is False
    assert tools.should_route_to_agent("qual processo está usando a porta 5173?") is True
    assert tools.should_route_to_agent("verifica o status do Git") is True
    assert tools.should_route_to_agent("o bloco de notas está aberto?") is True


def test_playback_metric_can_arrive_after_response_completion():
    telemetry = RealtimeTelemetry()
    telemetry.start("turn")
    telemetry.mark("turn", "t_response_complete")
    completed = telemetry.finish("turn")
    assert completed["playback_start_ms"] is None
    telemetry.playback_started("turn")
    assert telemetry.last_metrics["playback_start_ms"] is not None
    assert telemetry.last_metrics["speech_to_playback_ms"] == telemetry.last_metrics["playback_start_ms"]
