import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from app.conversation.engine import ConversationEngine
from app.core.config import Settings
from app.core.turn import TurnContext
from app.events import EventBus, EventType
from app.listening.manager import AlwaysListeningManager
from app.listening.models import PlaybackStateRequest
from app.natural_conversation.session import ConversationSession
from app.natural_conversation.speech_planner import Nonverbal, plan_speech
from app.realtime.telemetry import RealtimeTelemetry
from app.speech.online_providers import OpenAITtsProvider, ElevenLabsTtsProvider
from app.speech.queue import SpeechQueue, SpeechPriority
from app.speech.stream import AudioPacket
from app.speech.tts import TTSProvider, TtsCapabilities, FallbackTTSProvider


def test_session_never_confuses_generation_with_heard_words():
    session = ConversationSession()
    turn = TurnContext("é um S3", conversation_id=session.conversation_id)
    record = session.begin(turn, time.perf_counter())
    record.generated_text = "Esse modelo. Usa outro pino."
    record.chunks = {0: "Esse modelo.", 1: "Usa outro pino."}
    session.playback(PlaybackStateRequest(playing=True, response_id=turn.response_id, phase="started", chunk_index=0))
    assert record.spoken_text == ""
    assert session.playback(PlaybackStateRequest(playing=False, response_id=turn.response_id, phase="completed", chunk_index=0)) == "Esse modelo."
    assert session.playback(PlaybackStateRequest(playing=False, response_id=turn.response_id, phase="completed", chunk_index=0)) is None
    session.playback(PlaybackStateRequest(playing=False, response_id=turn.response_id, phase="interrupted", chunk_index=1,
                                        spoken_fraction=.4, barge_in_latency_ms=3.2))
    assert record.spoken_text == "Esse modelo."
    assert record.cancelled_text == "Usa outro pino."
    assert record.interrupted and record.spoken_fraction == .4
    next_turn = TurnContext("não, pera", conversation_id=session.conversation_id)
    session.begin(next_turn)
    assert "Resposta interrompida" in session.context()
    assert "Usa outro pino" not in session.context()
    assert session.snapshot()["metrics"]["BARGE_IN_DETECTION_TO_PLAYBACK_STOP"]["p50_ms"] == 3.2


def test_session_is_bounded_and_restart_cannot_reuse_stale_state():
    session = ConversationSession()
    for _ in range(70): session.begin(TurnContext("oi"))
    assert len(session.turns) == 40
    assert session.snapshot()["metrics"]["USER_SPEECH_END_TO_FIRST_AUDIO"]["count"] == 0
    session.close()
    new = ConversationSession()
    assert new.conversation_id != session.conversation_id and not new.turns
    assert new.snapshot()["states"] == ["LISTENING"]


@pytest.mark.parametrize("emotion", ["happy", "concerned", "amused", "serious"])
def test_planner_consumes_canonical_emotion_without_faking_local_acoustics(emotion):
    plan = plan_speech("[light_laugh] Certo.", emotion=emotion, intensity=.7,
                       capabilities=TtsCapabilities(), nonverbals=[Nonverbal.LIGHT_LAUGH])
    assert plan.emotion == emotion and plan.intensity == .7
    assert plan.spoken_text == "Certo." and not plan.acoustic_emotion
    assert plan.nonverbals == ["light_laugh"] and not plan.nonverbal_supported


@pytest.mark.asyncio
async def test_three_turns_share_session_and_capture_does_not_wait_for_model(tmp_path):
    settings = Settings.from_sources(database_path=tmp_path / "memory.db", natural_conversation_enabled=True,
                                     always_listening_enabled=True, voice_barge_in=True)
    bus = EventBus()
    listening = AlwaysListeningManager(settings, bus)
    await listening.acquire_lease("client_natural_test")
    release = asyncio.Event()
    seen = []
    async def converse(text, **kwargs):
        seen.append(kwargs["turn"].conversation_id)
        await release.wait()
    orchestrator = SimpleNamespace(converse=converse, emotion_planner=SimpleNamespace(configure=lambda **kw: None))
    engine = ConversationEngine(settings, bus, None, listening, orchestrator, RealtimeTelemetry())
    await engine.start()
    class Transcript:
        is_final = True
        def __init__(self, text): self.text = text
        def model_dump(self, **kwargs): return {"text": self.text}
    for text in ["oi", "como foi?", "tô cansado hoje"]:
        result = await asyncio.wait_for(engine.listening_audio_turn(None, "client_natural_test", transcription=Transcript(text)), .5)
        assert result["accepted"] and result["deferred"]
        assert listening.can_process("client_natural_test")[0]
    await asyncio.sleep(0)
    assert len(seen) == 3 and len(set(seen)) == 1
    assert not release.is_set()
    await bus.publish(EventType.TASK_STATE_CHANGED, task_id="task_controlled", source_turn=engine.session.turns[0].turn_id, state="RUNNING")
    assert "TOOL_RUNNING" in engine.session.snapshot()["states"]
    assert "LISTENING" in engine.session.snapshot()["states"]
    await bus.publish(EventType.TASK_STATE_CHANGED, task_id="task_controlled", source_turn=engine.session.turns[0].turn_id, state="COMPLETED")
    assert not engine.session.pending_tool_runs
    await engine.stop()
    assert engine.session.closed and not engine._voice_tasks


@pytest.mark.asyncio
async def test_interim_is_never_a_voice_turn():
    engine = ConversationEngine(SimpleNamespace(), EventBus(), None, None, SimpleNamespace(), RealtimeTelemetry())
    with pytest.raises(ValueError, match="Interim"):
        await engine._natural_turn(SimpleNamespace(is_final=False, text="um esp..."))
    assert not engine.session.turns


class Local(TTSProvider):
    name = "test_local"
    def __init__(self, path): self.path = path
    async def health(self): return True
    async def synthesize(self, text, state="neutral", options=None):
        if text == "slow": await asyncio.Event().wait()
        self.path.write_bytes(b"controlled-test-audio")
        return self.path


@pytest.mark.asyncio
async def test_barge_in_does_not_kill_queue_or_independent_notification(tmp_path):
    queue = SpeechQueue()
    provider = Local(tmp_path / "audio.wav")
    first = asyncio.create_task(queue.synthesize(provider, "slow", "neutral", response_id="old"))
    await asyncio.sleep(.01)
    second = asyncio.create_task(queue.synthesize(provider, "notification", "neutral", SpeechPriority.INFORMATIONAL, response_id="notice"))
    await asyncio.sleep(0)
    await queue.cancel("old")
    assert await asyncio.wait_for(second, .5) == provider.path
    assert first.cancelled()
    assert not queue._worker.done()
    await queue.stop()
    assert queue.pending == 0


class Credentials:
    def has_credential(self, provider): return True
    def get_for_authorized_provider(self, provider): return "unit-test-credential"


@pytest.mark.parametrize("provider_type, model, voice", [
    (OpenAITtsProvider, "gpt-4o-mini-tts", "coral"),
    (ElevenLabsTtsProvider, "eleven_multilingual_v2", "testVoiceId"),
])
@pytest.mark.asyncio
async def test_online_stream_is_incremental_and_cancellable(provider_type, model, voice):
    release = asyncio.Event()
    closed = []
    requests = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"\0\0" * 9600
            await release.wait()
            yield b"\0\0" * 9600
        async def aclose(self): closed.append(True)
    async def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=Stream())
    provider = provider_type(Credentials(), lambda: True, model=model, voice=voice,
                             transport=httpx.MockTransport(handler))
    stream = provider.stream_audio("Texto controlado.")
    first = await asyncio.wait_for(anext(stream), .5)
    assert first.pcm and not release.is_set()
    assert provider.capabilities().supports_streaming
    await stream.aclose()
    assert closed and not provider._active_tasks
    assert "unit-test-credential" not in str(provider.describe())
    assert "unit-test-credential" not in str(requests[0].url)


@pytest.mark.asyncio
async def test_stream_fallback_before_audio_only(tmp_path):
    class Remote(Local):
        async def stream_audio(self, text, state="neutral", options=None):
            if text == "partial": yield AudioPacket(pcm=b"\0\0")
            raise RuntimeError("controlled failure")
    fallback = FallbackTTSProvider(Remote(tmp_path / "unused"), Local(tmp_path / "local.wav"))
    packets = [p async for p in fallback.stream_audio("failure")]
    assert packets[0].path and fallback.last_used_fallback
    seen = []
    with pytest.raises(RuntimeError):
        async for packet in fallback.stream_audio("partial"): seen.append(packet)
    assert len(seen) == 1 and seen[0].pcm
    assert not fallback.last_used_fallback


@pytest.mark.asyncio
async def test_pcm_is_not_retained_as_event_history():
    bus = EventBus()
    await bus.publish(EventType.TTS_PCM_CHUNK, pcm="controlled")
    assert not bus.history()


@pytest.mark.asyncio
async def test_generation_finished_does_not_clear_real_playback_guard(tmp_path):
    settings = Settings.from_sources(database_path=tmp_path / "db", natural_conversation_enabled=True, voice_barge_in=True)
    bus = EventBus()
    listening = AlwaysListeningManager(settings, bus)
    await listening.start()
    await listening.playback(True)
    await bus.publish(EventType.TTS_FINISHED)
    assert listening.speaking
    assert not listening.can_process("invalid_lease")[0]
    await listening.stop()


@pytest.mark.asyncio
async def test_official_orchestrator_streams_and_saves_only_confirmed_voice(tmp_path):
    from test_realtime_v4 import (StreamingMock, RecordingTTS, MemoryRepository, V4SettingsManager,
        PCAwareness, AvatarController, VoiceProcessor, RealtimeOrchestrator, StateMachine)
    bus = EventBus()
    memory = MemoryRepository(tmp_path / "memory.db", bus)
    await memory.initialize()
    llm = StreamingMock()
    tts = RecordingTTS(tmp_path, llm)
    queue = SpeechQueue()
    config = V4SettingsManager(tmp_path / "v4.json")
    config.value.voice_processor.enabled = False
    telemetry = RealtimeTelemetry()
    orchestrator = RealtimeOrchestrator(llm, memory, StateMachine(memory, bus), bus, tts, queue,
        settings_manager=config, telemetry=telemetry,
        perception=PCAwareness(bus, config.value.realtime, config.value.privacy),
        avatar=AvatarController(bus), voice_processor=VoiceProcessor(config.value.voice_processor))
    settings = Settings.from_sources(database_path=tmp_path / "settings.db", natural_conversation_enabled=True)
    listening = AlwaysListeningManager(settings, bus)
    engine = ConversationEngine(settings, bus, None, listening, orchestrator, telemetry)
    await engine.start()
    turn = TurnContext("tô cansado hoje", conversation_id=engine.session.conversation_id)
    record = engine.session.begin(turn)
    result = await orchestrator.converse(turn.user_input, turn=turn)
    assert tts.started_before_llm_finished
    assert result.response and not record.spoken_text
    assert not any(v.role == "assistant" for v in await memory.recent_conversation())
    await engine.playback(PlaybackStateRequest(playing=True, response_id=turn.response_id, phase="started", chunk_index=0))
    assert await engine.speech_started()
    assert record.interrupted
    await engine.playback(PlaybackStateRequest(playing=False, response_id=turn.response_id, phase="completed", chunk_index=0))
    responses = [v.content for v in await memory.recent_conversation() if v.role == "assistant"]
    assert responses == [record.chunks[0]]
    await engine.stop()
    await queue.stop()


@pytest.mark.asyncio
async def test_cancellation_uses_owned_task_and_refuses_flash_cutoff():
    from app.natural_conversation.tool_bridge import cancellation_requested, cancel_session_task
    assert cancellation_requested("cancela isso") and not cancellation_requested("como cancela uma task?")
    session = ConversationSession()
    session.pending_tool_runs.add("task_owned")
    calls = []
    async def cancel(task_id): calls.append(task_id); return True
    goal = SimpleNamespace(task_id="task_owned", steps=[{"phase": "flashing"}])
    hardware = SimpleNamespace(goals={"goal": goal}, services=SimpleNamespace(intelligence=SimpleNamespace(tasks=SimpleNamespace(cancel=cancel))))
    assert "sensível" in await cancel_session_task(session, hardware)
    assert not calls
    goal.steps = [{"phase": "building"}]
    assert "Solicitei" in await cancel_session_task(session, hardware)
    assert calls == ["task_owned"] and not session.pending_tool_runs


@pytest.mark.parametrize("text", ["Como foi a conversa até agora?", "Estou cansado agora", "E agora?", "Oi"])
def test_casual_conversation_does_not_edit_recent_hardware_project(text):
    from app.hardware_engine.planner import understand
    assert understand(text, active_project=True) is None


@pytest.mark.parametrize("text", ["agora adiciona um botão", "agora muda o delay", "nesse projeto coloca um servidor Web"])
def test_explicit_project_continuation_remains_available_in_voice(text):
    from app.hardware_engine.planner import understand
    intent = understand(text, active_project=True)
    assert intent is not None and intent.project_only
