from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.local_transport import LocalRequestSecurityMiddleware
from app.core.runtime_settings import load_runtime_settings, save_runtime_settings
from app.events import EventBus
from app.speech.recognition import registry as registry_module
from app.speech.recognition.api import router
from app.speech.recognition.assembly import TranscriptAssembly
from app.speech.recognition.benchmark import score
from app.speech.recognition.credentials import STTCredentialBroker
from app.speech.recognition.deepgram import DeepgramSTTProvider, connection_failure, stream_options
from app.speech.recognition.local import FasterWhisperSTTProvider
from app.speech.recognition.models import AudioFormat, CanonicalTranscript, STTFailure, STTSettings, STTState, TranscriptWord
from app.speech.recognition.registry import STTProviderRegistry


SECRET = "fixture-credential-never-log-this"


class MemoryBroker:
    def __init__(self, configured=True):
        self.secret = SECRET if configured else None
        self.calls = []

    def resolve(self, credential_id):
        self.calls.append(credential_id)
        return self.secret

    def create(self, credential_id, secret, **kwargs):
        assert credential_id == "deepgram_api_key" and kwargs["operator_direct"]
        self.secret = secret
        return {"success": True}

    def delete(self, credential_id, **kwargs):
        assert kwargs["operator_direct"]
        self.secret = None


class LocalEngine:
    language = "pt"
    model_name = "existing-local-model"
    loaded = True

    def __init__(self):
        self.samples = []

    async def health(self):
        return True

    async def transcribe_pcm(self, audio, rate):
        self.samples.append((audio, rate))
        return SimpleNamespace(text="Conectei um ESP32.", language="pt")


def result(text="Conectei um ESP32.", *, final=True, speech_final=False, start=0, duration=1):
    return {"type": "Results", "start": start, "duration": duration,
            "is_final": final, "speech_final": speech_final,
            "channel": {"alternatives": [{"transcript": text, "confidence": .98,
                         "words": [{"word": "ESP32", "punctuated_word": "ESP32.", "start": start, "end": start + duration, "confidence": .95}]}]}}


class Socket:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.audio = []
        self.controls = []
        self.closed = False
        self.fail_send = False

    async def send(self, data):
        if self.fail_send:
            raise OSError(SECRET)
        if isinstance(data, bytes):
            self.audio.append(data)
            if len(self.audio) == 1:
                self.messages.put_nowait(json.dumps({"type": "SpeechStarted", "timestamp": 0}))
                self.messages.put_nowait(json.dumps(result("Conectei um", final=False)))
        else:
            self.controls.append(json.loads(data)["type"])
            if self.controls[-1] == "CloseStream":
                for item in (result(), result(), result("aqui.", start=1, speech_final=True),
                             {"type": "UtteranceEnd", "last_word_end": 2}, {"type": "Metadata"}, None):
                    self.messages.put_nowait(json.dumps(item) if item else None)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        return message


@pytest.fixture
def setup_registry():
    def create(configured=True, provider="deepgram"):
        settings = Settings(_env_file=None, stt_recognition=STTSettings(provider=provider))
        broker = MemoryBroker(configured)
        local = LocalEngine()
        saved = []
        registry = STTProviderRegistry(settings, local, STTCredentialBroker(broker), EventBus(), persist=saved.append)
        return registry, broker, local, saved
    return create


@pytest.fixture
def remote(monkeypatch):
    socket = Socket()
    connections = []

    async def connector(url, **options):
        connections.append((url, options))
        return socket

    def factory(*args, **kwargs):
        return DeepgramSTTProvider(*args, connector=connector, **kwargs)

    factory.capabilities = DeepgramSTTProvider.capabilities
    monkeypatch.setattr(registry_module, "DeepgramSTTProvider", factory)
    return socket, connections


def test_defaults_and_validated_limits():
    settings = STTSettings()
    assert settings.provider == "faster_whisper"  # cloud is opt-in
    assert settings.model == "nova-3" and settings.language == "pt-BR"
    assert settings.smart_format and settings.numerals and settings.punctuate
    assert settings.interim_results and settings.vad_events
    assert settings.endpointing == 300 and settings.utterance_end_ms == 1000
    assert not settings.profanity_filter and not settings.diarize and not settings.keyterms_enabled
    for bad in ({"endpointing": 2}, {"utterance_end_ms": 900}, {"keyterms": ["a"] * 21},
                {"api_key": SECRET}, {"model": "self-hosted"}, {"language": "pt&token=secret"}):
        with pytest.raises(ValidationError):
            STTSettings(**bad)


def test_options_and_capabilities_are_honest():
    values = dict(stream_options(STTSettings(), AudioFormat()))
    assert values["model"] == "nova-3" and values["language"] == "pt-BR"
    assert values["sample_rate"] == "48000" and values["encoding"] == "linear16"
    assert values["numerals"] == values["smart_format"] == "true"
    assert values["profanity_filter"] == "false" and "redact" not in values
    assert "utterance_end_ms" not in dict(stream_options(STTSettings(interim_results=False), AudioFormat()))
    assert "keyterm" not in values
    terms = stream_options(STTSettings(keyterms_enabled=True, keyterms=["KAZUMI", "ESP32"]), AudioFormat(), ["GPIO"])
    assert [value for key, value in terms if key == "keyterm"] == ["KAZUMI", "ESP32", "GPIO"]
    assert DeepgramSTTProvider.capabilities(None).streaming
    assert not FasterWhisperSTTProvider.capabilities(None).streaming
    assert not FasterWhisperSTTProvider.capabilities(None).interim_results


def test_credentials_survive_new_facade_without_metadata_index():
    broker = MemoryBroker(False)
    credentials = STTCredentialBroker(broker)
    assert not credentials.configured()
    credentials.save(SECRET)
    assert STTCredentialBroker(broker).configured()
    assert credentials.resolve() == SECRET
    assert set(broker.calls) == {"deepgram_api_key"}
    credentials.remove()
    assert not credentials.configured()
    assert not STTCredentialBroker(None).configured()


async def test_protocol_lifecycle_parsing_and_assembly(setup_registry, remote):
    registry, _, local, _ = setup_registry()
    socket, connections = remote
    events = []

    async def sink(event):
        events.append(event)

    session = await registry.open_session(AudioFormat(), sink)
    await session.send_audio(b"\x00\x01" * 4096)
    final = await session.finish()
    assert final.text == "Conectei um ESP32. aqui." and final.is_final
    assert final.provider == "deepgram" and final.speech_final
    assert final.language == "pt-BR" and final.words and final.confidence == .98
    assert session.duplicates == 1
    assert {item["type"] for item in events} >= {"interim", "final", "speech_started", "speech_final", "utterance_end"}
    assert not local.samples
    url, options = connections[0]
    assert SECRET not in url and options["additional_headers"] == {"Authorization": "Token " + SECRET}
    assert not options["logger"].propagate and not options["logger"].isEnabledFor(10)
    assert parse_qs(urlsplit(url).query)["language"] == ["pt-BR"]
    assert session.diagnostics()["mic_to_first_interim_ms"] is not None
    assert await session.finish() is final
    await session.close()
    assert socket.closed and registry.active is None and not session.audio and session.queue.empty()
    assert session.provider.receiver is None and session.provider.keepalive is None
    assert SECRET not in json.dumps(await registry.status())


async def test_missing_credentials_offline_startup_and_restart(setup_registry, remote):
    registry, broker, local, _ = setup_registry(False)
    assert await registry.health()
    assert (await registry.status())["deepgram_state"] == "NOT_CONFIGURED"
    for _ in range(2):
        session = await registry.open_session(AudioFormat(), AsyncMock())
        await session.send_audio(b"\x01\x00" * 4096)
        final = await session.finish()
        assert final.provider == "faster_whisper"
        assert session.state == STTState.FALLBACK
        await session.close()
    assert len(local.samples) == 2 and not remote[1]
    await registry.close()
    assert registry.closed and not registry.active


async def test_midstream_network_failure_replays_identical_audio_once(setup_registry, remote):
    registry, _, local, _ = setup_registry()
    socket, _ = remote
    first, second = b"\x01\x00" * 4096, b"\x02\x00" * 4096
    session = await registry.open_session(AudioFormat(), AsyncMock())
    await session.send_audio(first)
    await session.queue.join()
    # A finalized remote segment has not been submitted to chat.
    await session.provider.parse_message(result())
    socket.fail_send = True
    await session.send_audio(second)
    final = await session.finish()
    assert final.provider == "faster_whisper" and final.text == "Conectei um ESP32."
    assert local.samples == [(first + second, 48000)]
    assert registry.deepgram_state == STTState.NETWORK_ERROR
    assert registry.retry_at > 0
    assert SECRET not in registry.last_failure
    await session.close()


@pytest.mark.parametrize("status,state", [(401, STTState.AUTH_ERROR), (403, STTState.AUTH_ERROR), (429, STTState.NETWORK_ERROR), (500, STTState.NETWORK_ERROR), (400, STTState.ERROR)])
async def test_auth_and_network_error_fallback_sanitized(setup_registry, monkeypatch, status, state):
    registry, _, local, _ = setup_registry()
    error = OSError(SECRET)
    error.response = SimpleNamespace(status_code=status)

    async def connect(*args, **kwargs):
        raise error

    monkeypatch.setattr(registry_module, "DeepgramSTTProvider", lambda *args, **kwargs: DeepgramSTTProvider(*args, connector=connect, **kwargs))
    session = await registry.open_session(AudioFormat(), AsyncMock())
    await session.send_audio(b"\x00\x00" * 400)
    assert (await session.finish()).provider == "faster_whisper"
    assert registry.deepgram_state == state
    assert SECRET not in registry.last_failure
    assert local.samples
    await session.close()


async def test_switch_during_capture_keeps_audio_and_persists(setup_registry, remote):
    registry, _, local, saved = setup_registry()
    session = await registry.open_session(AudioFormat(), AsyncMock())
    audio = b"\x01\x00" * 1024
    await session.send_audio(audio)
    await session.queue.join()
    await registry.update(STTSettings(provider="faster_whisper", endpointing=600))
    assert remote[0].closed
    await session.send_audio(audio)
    assert (await session.finish()).provider == "faster_whisper"
    assert local.samples == [(audio + audio, 48000)]
    assert saved[-1]["stt_recognition"]["endpointing"] == 600
    await session.close()
    new_session = await registry.open_session(AudioFormat(), AsyncMock())
    assert new_session.utterance_id != session.utterance_id
    await new_session.close()


async def test_settings_change_before_worker_starts_cannot_open_old_remote(setup_registry, remote):
    registry, _, _, _ = setup_registry()
    session = await registry.open_session(AudioFormat(), AsyncMock())
    await session.force_fallback("credential removed")
    await session.send_audio(b"\x01\x00" * 100)
    assert (await session.finish()).provider == "faster_whisper"
    assert not remote[1]
    await session.close()


async def test_backpressure_preserves_full_sample_and_limits(setup_registry, remote):
    registry, _, local, _ = setup_registry(False)
    session = await registry.open_session(AudioFormat(), AsyncMock())
    for _ in range(40):
        await session.send_audio(b"\x00\x01" * 4096)
    final = await session.finish()
    assert final.provider == "faster_whisper" and session.queue_overflows > 0
    assert local.samples[0][0] == b"\x00\x01" * (4096 * 40)
    await session.close()
    session = await registry.open_session(AudioFormat(), AsyncMock())
    with pytest.raises(STTFailure, match="Invalid PCM"):
        await session.send_audio(b"odd")
    with pytest.raises(STTFailure, match="Invalid PCM"):
        await session.send_audio(b"\x00" * 40000)
    await session.close()


async def test_exclusive_session_ticket_single_use_and_shutdown(setup_registry):
    registry, _, _, _ = setup_registry(False)
    ticket = registry.issue_ticket({"mode": "direct"})
    assert registry.consume_ticket(ticket) == {"mode": "direct"}
    with pytest.raises(STTFailure):
        registry.consume_ticket(ticket)
    session = await registry.open_session(AudioFormat(), AsyncMock())
    with pytest.raises(STTFailure):
        await registry.open_session(AudioFormat(), AsyncMock())
    await registry.close()
    assert session.closed and session.worker.done() and not registry.active


def test_settings_roundtrip_and_benchmark_metrics(tmp_path):
    path = tmp_path / "settings.json"
    config = STTSettings(provider="deepgram", endpointing=700, interim_results=False)
    save_runtime_settings({"stt_recognition": config.model_dump()}, path)
    assert STTSettings.model_validate(load_runtime_settings(path)["stt_recognition"]) == config
    assert SECRET not in path.read_text()
    assert score("Conectei um ESP32", "Conectei um ESP32")["wer"] == 0
    assert score("Conectei um ESP32", "Conectei um cabo")["wer"] == pytest.approx(1 / 3, abs=.001)
    assert score("Conectei um ESP32", "Conectei um cabo")["technical_terms"]["accuracy"] == 0
    assert score("", "Fala livre")["wer"] is None


@pytest.fixture
def api_client(setup_registry):
    registry, broker, local, _ = setup_registry(False, "faster_whisper")
    app = FastAPI()
    app.add_middleware(LocalRequestSecurityMiddleware, frontend_port=5173, backend_port=8000)
    app.state.services = SimpleNamespace(stt=registry, conversation=SimpleNamespace(
        direct_audio_turn=AsyncMock(return_value={"accepted": True}),
        listening_audio_turn=AsyncMock(return_value={"accepted": True})),
        listening=SimpleNamespace(can_process=lambda client_id: (client_id == "valid-client", "lease_required")))
    app.include_router(router)
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 12345)) as client:
        yield client, app.state.services, broker


def test_api_secret_never_returned_even_in_validation_errors(api_client):
    client, services, broker = api_client
    for payload in ({"api_key": SECRET, "extra": SECRET}, {"api_key": {"secret": SECRET}}):
        response = client.put("/api/stt/credential", json=payload)
        assert response.status_code == 422 and SECRET not in response.text
    response = client.put("/api/stt/credential", json={"api_key": SECRET})
    assert response.status_code == 200 and SECRET not in response.text
    assert response.json()["settings"]["provider"] == "deepgram"
    assert response.json()["credential_configured"]
    assert SECRET not in client.get("/api/stt/settings").text
    assert client.delete("/api/stt/credential").status_code == 200
    assert broker.secret is None
    assert client.post("/api/stt/ticket", json={}, headers={"origin": "https://untrusted.example"}).status_code == 403


def test_stream_api_final_only_diagnostic_and_benchmark(api_client):
    client, services, _ = api_client
    response = client.post("/api/stt/ticket", json={"mode": "diagnostic", "benchmark": True, "reference": "Conectei um ESP32."})
    ticket = response.json()["ticket"]
    with client.websocket_connect("/api/stt/stream") as socket:
        socket.send_json({"ticket": ticket})
        assert socket.receive_json()["type"] == "ready"
        socket.send_bytes(b"\x00\x01" * 4096)
        socket.send_json({"type": "end"})
        while True:
            event = socket.receive_json()
            if event["type"] == "result":
                break
        assert event["result"]["transcription"]["is_final"]
        assert event["result"]["comparison"]["audio"] == "SAME SAMPLE"
        assert event["result"]["comparison"]["deepgram"] is None
    assert services.conversation.direct_audio_turn.await_count == 0
    assert services.conversation.listening_audio_turn.await_count == 0
    assert services.stt.active is None


def test_direct_stream_submits_exactly_once_and_listening_needs_lease(api_client):
    client, services, _ = api_client
    assert client.post("/api/stt/ticket", json={"mode": "listening", "client_id": "bad-client"}).status_code == 409
    ticket = client.post("/api/stt/ticket", json={"mode": "direct"}).json()["ticket"]
    with client.websocket_connect("/api/stt/stream") as socket:
        socket.send_json({"ticket": ticket})
        assert socket.receive_json()["type"] == "ready"
        socket.send_bytes(b"\x00\x01" * 4096)
        socket.send_json({"type": "end"})
        while socket.receive_json()["type"] != "result":
            pass
    assert services.conversation.direct_audio_turn.await_count == 1
    transcript = services.conversation.direct_audio_turn.call_args.kwargs["transcription"]
    assert transcript.is_final and transcript.text == "Conectei um ESP32."


def test_disconnect_cleans_session_without_turn(api_client):
    client, services, _ = api_client
    ticket = client.post("/api/stt/ticket", json={"mode": "direct"}).json()["ticket"]
    with client.websocket_connect("/api/stt/stream") as socket:
        socket.send_json({"ticket": ticket})
        assert socket.receive_json()["type"] == "ready"
        socket.send_bytes(b"\x00\x00" * 100)
        socket.send_json({"type": "cancel"})
    assert services.stt.active is None
    assert services.conversation.direct_audio_turn.await_count == 0


async def test_keepalive_is_text_and_worker_stops():
    socket = Socket()
    provider = DeepgramSTTProvider(STTSettings(), AudioFormat(), STTCredentialBroker(MemoryBroker()),
                                   "test", AsyncMock(), connector=AsyncMock(return_value=socket))
    await provider.connect()
    provider.last_sent -= 5
    await asyncio.sleep(3.1)
    assert "KeepAlive" in socket.controls
    await provider.close()
    assert provider.receiver is None and provider.keepalive is None and socket.closed


async def test_finalization_without_metadata_replays_locally(setup_registry, remote):
    registry, _, local, _ = setup_registry()
    session = await registry.open_session(AudioFormat(), AsyncMock())
    await session.send_audio(b"\x00\x01" * 100)
    await session.queue.join()
    original_send = remote[0].send

    async def truncated(data):
        if isinstance(data, str) and 'CloseStream' in data:
            remote[0].messages.put_nowait(json.dumps(result()))
            remote[0].messages.put_nowait(None)
        else:
            await original_send(data)

    remote[0].send = truncated
    final = await session.finish()
    assert final.provider == "faster_whisper" and len(local.samples) == 1
    assert registry.last_failure == "Deepgram finalization incomplete"
    await session.close()


async def test_receiver_disconnect_sets_health_before_next_frame(setup_registry, remote):
    registry, _, _, _ = setup_registry()
    session = await registry.open_session(AudioFormat(), AsyncMock())
    await session.send_audio(b"\x00\x00" * 100)
    await session.queue.join()
    remote[0].messages.put_nowait(None)
    await session.provider.receiver
    assert registry.deepgram_state == STTState.NETWORK_ERROR
    assert session.state == STTState.DEGRADED
    assert (await session.finish()).provider == "faster_whisper"
    await session.close()


def test_overlap_and_intentional_repetition_use_timestamps():
    def segment(text, start, end, words):
        return CanonicalTranscript(text=text, is_final=True, provider="deepgram", language="pt-BR",
            utterance_id="test", sequence=1, started_at=start, ended_at=end,
            words=[TranscriptWord(text=t, started_at=s, ended_at=e) for t, s, e in words])
    assembly = TranscriptAssembly()
    first = segment("ESP32 conectado", 0, 2, [("ESP32", 0, 1), ("conectado", 1, 2)])
    assert assembly.add(first)
    assert assembly.add(segment("conectado aqui", 1, 3, [("conectado", 1, 2), ("aqui", 2, 3)]))
    assert assembly.add(segment("ESP32", 3, 4, [("ESP32", 3, 4)]))
    assert assembly.finish(first).text == "ESP32 conectado aqui ESP32"
    assert assembly.duplicates == 1


async def test_stream_interims_do_not_enter_event_history(setup_registry, remote):
    registry, _, _, _ = setup_registry()
    session = await registry.open_session(AudioFormat(), AsyncMock())
    await session.send_audio(b"\x00\x00" * 100)
    await session.finish()
    assert not registry.event_bus._history
    await session.close()


async def test_canonical_interim_is_rejected_by_official_conversation():
    from app.conversation.engine import ConversationEngine
    engine = ConversationEngine(SimpleNamespace(), EventBus(), None, None, None, None)
    interim = CanonicalTranscript(text="partial", is_final=False, provider="deepgram", language="pt-BR", utterance_id="test", sequence=1)
    with pytest.raises(ValueError, match="Interim"):
        await engine.direct_audio_turn(None, transcription=interim)
