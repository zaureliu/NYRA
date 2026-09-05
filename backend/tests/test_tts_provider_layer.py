from __future__ import annotations

import asyncio
import io
import json
import wave
from pathlib import Path

import httpx
import pytest

from app.speech.online_providers import ElevenLabsTtsProvider, OpenAITtsProvider
from app.core.config import Settings
from app.core.runtime_settings import load_runtime_settings, save_runtime_settings
from app.speech.profile import VoiceSynthesisOptions
from app.speech.prosody import SpeechTextNormalizer
from app.speech.provider_credentials import TtsCredentialBroker
from app.speech.provider_models import TtsProviderStatus
from app.speech.provider_registry import LocalTtsProvider, TtsProviderRegistry
from app.speech.queue import SpeechQueue
from app.speech.tts import TTSProvider, TtsCapabilities


def wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(b"\x00\x00" * 240)
    return output.getvalue()


class FakeCredentials:
    def __init__(self, **values: str) -> None:
        self.values = values

    def has_credential(self, provider_id: str) -> bool:
        return bool(self.values.get(provider_id))

    def get_for_authorized_provider(self, provider_id: str) -> str | None:
        return self.values.get(provider_id)


class FakeLocal(TTSProvider):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[str] = []
        self.cancelled = False

    @property
    def name(self) -> str:
        return "fake-local-engine"

    @property
    def default_voice(self) -> str:
        return "nyra-local"

    @property
    def voices(self) -> list[dict[str, str]]:
        return [{"id": "nyra-local", "name": "Nyra local", "language": "pt-BR"}]

    def capabilities(self) -> TtsCapabilities:
        return TtsCapabilities(pt_br=True, offline=True, voice_selection=True)

    async def health(self) -> bool:
        return True

    async def cancel(self) -> None:
        self.cancelled = True

    async def synthesize(self, text: str, state: str = "neutral", options=None) -> Path:
        del state, options
        self.calls.append(text)
        destination = self.root / f"local-{len(self.calls)}.wav"
        destination.write_bytes(wav_bytes())
        return destination.resolve()


def registry_with_openai(
    tmp_path: Path,
    handler,
    *,
    online_enabled: bool,
    configured: bool = True,
) -> tuple[TtsProviderRegistry, FakeLocal, OpenAITtsProvider, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    credentials = FakeCredentials(openai="fixture-secret" if configured else "")
    local = FakeLocal(tmp_path)
    registry = TtsProviderRegistry(
        LocalTtsProvider(local),
        credentials,  # type: ignore[arg-type]
        selected_provider="openai",
        online_enabled=online_enabled,
    )
    provider = OpenAITtsProvider(
        credentials,  # type: ignore[arg-type]
        lambda: registry.online_enabled,
        model="gpt-4o-mini-tts",
        voice="coral",
        timeout_seconds=0.2,
        transport=httpx.MockTransport(recording_handler),
    )
    provider.output_dir = tmp_path
    registry.register(provider)
    return registry, local, provider, requests


@pytest.mark.asyncio
async def test_default_local_provider_is_offline_and_ready(tmp_path: Path) -> None:
    credentials = FakeCredentials()
    registry = TtsProviderRegistry(LocalTtsProvider(FakeLocal(tmp_path)), credentials)  # type: ignore[arg-type]
    metadata = await registry.provider_metadata()

    assert registry.configured_provider == "local"
    assert registry.active_provider == "local"
    assert registry.online_enabled is False
    assert metadata["providers"][0]["status"] == "LOCAL_READY"
    assert metadata["providers"][0]["capabilities"]["offline"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,expected", [(True, "DISABLED"), (False, "NOT_CONFIGURED")])
async def test_disabled_or_missing_credential_never_calls_network_and_falls_back(
    tmp_path: Path, configured: bool, expected: str
) -> None:
    registry, local, _provider, requests = registry_with_openai(
        tmp_path,
        lambda request: pytest.fail(f"network request was not allowed: {request.url}"),
        online_enabled=not configured,
        configured=configured,
    )

    output = await registry.synthesize("Somente texto destinado à fala.")

    assert output.is_file()
    assert local.calls == ["Somente texto destinado à fala."]
    assert requests == []
    assert registry.fallback_active is True
    assert registry.fallback_reason == expected


@pytest.mark.asyncio
async def test_openai_payload_is_minimal_and_style_is_provider_owned(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert request.url.path == "/v1/audio/speech"
        assert request.headers["authorization"] == "Bearer fixture-secret"
        return httpx.Response(200, content=wav_bytes(), headers={"x-request-id": "fixture-request"})

    registry, local, provider, requests = registry_with_openai(tmp_path, handler, online_enabled=True)
    options = VoiceSynthesisOptions(
        emotion="focused",
        emotion_intensity=0.3,
        style_instruction="Fale com foco sereno.",
    )

    output = await registry.synthesize("Texto final preparado para fala.", options=options)

    assert output.is_file()
    assert local.calls == []
    assert len(requests) == 1
    assert set(captured) == {"model", "input", "voice", "response_format", "speed", "instructions"}
    assert captured["input"] == "Texto final preparado para fala."
    assert captured["model"] == "gpt-4o-mini-tts"
    assert captured["voice"] == "coral"
    assert captured["instructions"] == "Fale com foco sereno."
    assert all(key not in captured for key in ("messages", "memory", "system", "tools", "history", "rag"))
    assert provider.last_request_id == "fixture-request"
    assert registry.active_provider == "openai"
    assert provider.capabilities().supports_streaming is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (401, {"error": "invalid"}, TtsProviderStatus.AUTH_ERROR),
        (429, {"error": {"code": "insufficient_quota"}}, TtsProviderStatus.QUOTA_ERROR),
        (500, {"error": "temporary"}, TtsProviderStatus.NETWORK_ERROR),
    ],
)
async def test_remote_failures_are_classified_and_fall_back_once(
    tmp_path: Path, status_code: int, body: dict, expected: TtsProviderStatus
) -> None:
    registry, local, provider, requests = registry_with_openai(
        tmp_path,
        lambda _request: httpx.Response(status_code, json=body),
        online_enabled=True,
    )

    output = await registry.synthesize("Fallback seguro.")

    assert output.is_file()
    assert len(requests) == 1
    assert local.calls == ["Fallback seguro."]
    assert registry.fallback_reason == expected.value
    assert provider.last_status == expected


@pytest.mark.asyncio
async def test_elevenlabs_payload_uses_voice_path_and_no_context(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert request.url.path == "/v1/text-to-speech/voice_fixture_123"
        assert request.url.params["output_format"] == "mp3_44100_128"
        assert request.headers["xi-api-key"] == "fixture-secret"
        return httpx.Response(200, content=b"fixture-mp3", headers={"request-id": "eleven-request"})

    def fake_decode(_data: bytes, destination: Path) -> Path:
        destination.write_bytes(wav_bytes())
        return destination.resolve()

    monkeypatch.setattr("app.speech.online_providers.decode_audio_response_to_wav", fake_decode)
    credentials = FakeCredentials(elevenlabs="fixture-secret")
    provider = ElevenLabsTtsProvider(
        credentials,  # type: ignore[arg-type]
        lambda: True,
        model="eleven_multilingual_v2",
        voice="voice_fixture_123",
        transport=httpx.MockTransport(handler),
    )
    provider.output_dir = tmp_path

    output = await provider.synthesize(
        "Texto isolado.",
        options=VoiceSynthesisOptions(emotion="calm", emotion_intensity=0.4),
    )

    assert output.is_file()
    assert set(captured) == {"text", "model_id", "voice_settings"}
    assert captured["text"] == "Texto isolado."
    assert captured["model_id"] == "eleven_multilingual_v2"
    assert provider.last_request_id == "eleven-request"
    assert provider.capabilities().voice_cloning is False
    assert provider.capabilities().supports_streaming is True


@pytest.mark.asyncio
async def test_provider_switch_is_hot_and_old_delayed_audio_is_cancelled(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def delayed_handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(10)
        return httpx.Response(200, content=wav_bytes())

    registry, local, provider, _requests = registry_with_openai(
        tmp_path, delayed_handler, online_enabled=True
    )
    queue = SpeechQueue()
    old = asyncio.create_task(
        queue.synthesize(provider, "Resposta velha.", "neutral", response_id="turn-old", turn_id="turn-old")
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    cancelled = await queue.cancel("turn-old")
    registry.configure(selected_provider="local")
    await registry.cancel()

    with pytest.raises(asyncio.CancelledError):
        await old
    current = await queue.synthesize(registry, "Resposta atual.", "neutral", response_id="turn-new", turn_id="turn-new")
    await queue.stop()

    assert cancelled == 1
    assert current.is_file()
    assert local.calls == ["Resposta atual."]
    assert registry.active_provider == "local"
    assert queue.counters["tts_items_cancelled"] >= 1
    assert not list(tmp_path.glob("nyra-openai-*.wav"))


def test_tts_credential_wrapper_never_returns_the_secret() -> None:
    class RawBroker:
        def __init__(self) -> None:
            self.secret = "fixture-secret"
            self.created: tuple | None = None

        def status(self, credential_id: str) -> dict:
            return {"success": credential_id == "tts_openai_api_key"}

        def create(self, *args, **kwargs) -> dict:
            self.created = (args, kwargs)
            return {"success": True, "secret": self.secret}

        def delete(self, *args, **kwargs) -> dict:
            return {"success": True, "secret": self.secret}

        def resolve(self, credential_id: str) -> str:
            assert credential_id == "tts_openai_api_key"
            return self.secret

    raw = RawBroker()
    broker = TtsCredentialBroker(raw)  # type: ignore[arg-type]

    response = broker.save_credential("openai", raw.secret)
    removed = broker.delete_credential("openai")

    assert broker.has_credential("openai") is True
    assert response == {"success": True, "credential_id": "tts_openai_api_key", "configured": True}
    assert removed == {"success": True, "configured": False}
    assert raw.secret not in json.dumps(response)
    assert raw.secret not in json.dumps(removed)
    assert raw.created is not None
    assert raw.created[1]["operator_direct"] is True


def test_speech_normalizer_removes_private_runtime_material() -> None:
    source = """## Resposta
SYSTEM: prompt privado completo
TOOL_RESULT: resultado privado
PID=4242 HWND=0xA11 MONITOR_ID=display-3
Veja https://example.invalid/private e C:\\Users\\Operator\\secret.txt.
```json
{"memory": "private", "rag": ["chunk"]}
```
Texto natural que realmente será falado.
"""

    speech = SpeechTextNormalizer().prepare(source).speech_text.casefold()

    for forbidden in ("prompt privado", "resultado privado", "4242", "0xa11", "example.invalid", "operator", "memory", "rag"):
        assert forbidden not in speech
    assert "texto natural" in speech
    assert "caminho disponível na tela" in speech


def test_provider_settings_persist_across_restart_without_credentials(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings-v33.json"
    stored = save_runtime_settings(
        {
            "tts_provider_id": "elevenlabs",
            "tts_provider_fallback": "local",
            "tts_online_enabled": True,
            "tts_elevenlabs_model": "eleven_multilingual_v2",
            "tts_elevenlabs_voice_id": "voice_fixture_123",
        },
        settings_path,
    )
    restarted = Settings.from_sources(**load_runtime_settings(settings_path))

    assert stored["tts_provider_id"] == "elevenlabs"
    assert restarted.tts_provider_id == "elevenlabs"
    assert restarted.tts_provider_fallback == "local"
    assert restarted.tts_online_enabled is True
    assert restarted.tts_elevenlabs_voice_id == "voice_fixture_123"
    assert all("api_key" not in key and "credential" not in key for key in stored)


def test_fresh_install_provider_defaults_are_local_and_online_off() -> None:
    settings = Settings()

    assert settings.tts_provider_id == "local"
    assert settings.tts_provider_fallback == "local"
    assert settings.tts_online_enabled is False
