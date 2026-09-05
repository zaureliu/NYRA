"""Controlled contracts; local fixtures are NOT proof of Gradium cloud audio."""
import asyncio
import base64
import json
import re
from types import SimpleNamespace

import httpx
import pytest
from websockets.asyncio.server import serve

from app.core.config import Settings
from app.core.runtime_settings import load_runtime_settings, save_runtime_settings
from app.speech.custom_provider import CustomTTSProvider
from app.speech.gradium_provider import GradiumTTSProvider
from app.speech.provider_credentials import TtsCredentialBroker
from app.speech.provider_models import TtsProviderError
from app.speech.provider_registry import build_tts_provider_registry
from app.speech.provider_transport import NoRedirectConnect, decode_buffered, open_socket, resolve_endpoint
from app.speech.queue import SpeechQueue
from app.speech.synthesis_config import CustomProfile, GradiumSettings, UniversalTtsSettings, substitute, validate_template
from test_tts_provider_layer import FakeCredentials, FakeLocal, wav_bytes


def profile(**updates):
    return CustomProfile.model_validate({"id": "test-provider", "name": "Controlled fixture", "endpoint": "https://tts.example/speech", **updates})


@pytest.mark.parametrize("url", ["file:///secret", "javascript:alert(1)", "shell:cmd", "exec:binary", "https://user:secret@tts.example/", "https://tts.example/?token=secret", "https://tts.example/#secret", "http://tts.example", "https://192.168.1.1", "https://169.254.169.254", "http://127.0.0.1"])
def test_endpoint_rejection(url):
    with pytest.raises(ValueError):
        profile(endpoint=url)


@pytest.mark.parametrize("value", [{"Authorization": "secret"}, {"api_key": "secret"}, {"nested": [{"token": "secret"}]}, {"text": "{{__import__('os')}}"}, {"text": "{{unknown}}"}, {"text": "Bearer abcdefg"}])
def test_template_rejection(value):
    with pytest.raises(ValueError):
        profile(request_template=value)


def test_safe_typed_substitution_no_evaluation():
    template = {"text": "{{text}}", "rate": "{{sample_rate}}", "nested": ["Voice: {{voice_id}}"]}
    validate_template(template)
    text = 'Quotes " } , malicious() {{unknown}}'
    value = substitute(template, {"text": text, "sample_rate": 48000, "voice_id": "v"})
    assert value == {"text": text, "rate": 48000, "nested": ["Voice: v"]}
    assert json.loads(json.dumps(value))["text"] == text


@pytest.mark.asyncio
async def test_dns_private_rebinding_blocked(monkeypatch):
    async def resolve(*args, **kwargs):
        return [(2, 1, 6, "", ("127.0.0.1", 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(ValueError):
        await resolve_endpoint("https://public.example/tts", "rest")
    with pytest.raises(ValueError):
        profile(allow_loopback=True)


def test_redirects_never_followed():
    marker = ValueError("redirect")
    assert NoRedirectConnect("wss://tts.example").process_redirect(marker) is marker


def test_broker_scoped_ids_export_and_persistence(tmp_path):
    a, b = [TtsCredentialBroker.credential_id(v) for v in ("custom:test-provider", "custom:test_provider")]
    assert a != b and re.fullmatch(r"[a-z0-9_]{3,64}", a)
    assert TtsCredentialBroker.credential_id("gradium") == "gradium_api_key"
    with pytest.raises(Exception):
        TtsCredentialBroker.credential_id("custom:../other")
    value = UniversalTtsSettings(custom_profiles=[profile()], active_custom_profile="test-provider",
        gradium=GradiumSettings(voice_id="voice-ref"))
    path = tmp_path / "settings.json"
    save_runtime_settings({"tts_provider_id": "gradium", "tts_universal": value.model_dump(mode="json")}, path)
    loaded = Settings.from_sources(**load_runtime_settings(path))
    assert loaded.tts_universal == value and loaded.tts_provider_id == "gradium"
    assert "credential" not in path.read_text() and "fixture-secret" not in path.read_text()
    with pytest.raises(ValueError):
        UniversalTtsSettings(custom_profiles=[profile(), profile()])


@pytest.mark.asyncio
async def test_registry_offline_missing_credentials_and_all_five(tmp_path):
    registry = build_tts_provider_registry(Settings(tts_provider_id="gradium"), FakeLocal(tmp_path), FakeCredentials())
    result = await registry.provider_metadata()
    assert [p["id"] for p in result["providers"]] == ["local", "openai", "elevenlabs", "gradium", "custom"]
    assert result["providers"][3]["status"] == "NOT_CONFIGURED"
    assert (await registry.synthesize("Offline.")).exists()
    await registry.close()


@pytest.mark.asyncio
async def test_gradium_real_local_ws_incremental_text_pcm_reuse_timestamps():
    connections, requests = [], []
    async def server(ws):
        connections.append(ws)
        assert ws.request.headers["x-api-key"] == "fixture-secret"
        async for raw in ws:
            msg = json.loads(raw); requests.append(msg)
            req = msg["client_req_id"]
            if msg["type"] == "setup":
                await ws.send(json.dumps({"type": "ready", "sample_rate": 48000, "client_req_id": req}))
            elif msg["type"] == "text":
                await ws.send(json.dumps({"type": "audio", "audio": base64.b64encode(b"\x01\x00" * 480).decode(), "client_req_id": req}))
                await ws.send(json.dumps({"type": "text", "text": "Oi.", "start_s": 0, "stop_s": .01, "client_req_id": req}))
            elif msg["type"] == "end_of_stream":
                await ws.send(json.dumps({"type": "end_of_stream", "client_req_id": req}))
    async with serve(server, "127.0.0.1", 0) as endpoint:
        async def connector(url, headers):
            assert url == "wss://api.gradium.ai/api/speech/tts"
            return await open_socket(f"ws://127.0.0.1:{endpoint.sockets[0].getsockname()[1]}", headers, True)
        provider = GradiumTTSProvider(FakeCredentials(gradium="fixture-secret"), lambda: True, GradiumSettings(voice_id="test-voice"), connector=connector)
        release = asyncio.Event()
        async def tokens():
            yield "Oi."
            await release.wait()
            yield "Segundo trecho."
        stream = provider.stream_text(tokens())
        first = await asyncio.wait_for(anext(stream), 2)
        assert first.pcm and first.sample_rate == 48000
        assert not any(m["type"] == "end_of_stream" for m in requests)
        release.set()
        rest = [p async for p in stream]
        assert any(p.timestamps for p in rest)
        assert requests[0]["close_ws_on_eos"] is False
        assert requests[0]["model_name"] == "default" and requests[0]["output_format"] == "pcm"
        assert not any(key in requests[0] for key in ("emotion", "style", "api_key"))
        assert [p async for p in provider.stream_audio("Reutilização.")]
        assert len(connections) == 1
        assert provider.latency()["samples"] == 2
        await provider.close()
        assert provider._socket is None and not provider._active_tasks


@pytest.mark.asyncio
async def test_gradium_barge_in_closes_socket_and_queue_survives(tmp_path):
    connected = asyncio.Event()
    async def server(ws):
        setup = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "ready", "sample_rate": 48000, "client_req_id": setup["client_req_id"]}))
        connected.set()
        await ws.wait_closed()
    async with serve(server, "127.0.0.1", 0) as endpoint:
        async def connector(url, headers):
            return await open_socket(f"ws://127.0.0.1:{endpoint.sockets[0].getsockname()[1]}", headers, True)
        provider = GradiumTTSProvider(FakeCredentials(gradium="fixture-secret"), lambda: True, GradiumSettings(voice_id="test"), connector=connector)
        queue = SpeechQueue()
        async def audio(packet):
            pass
        pending = asyncio.create_task(queue.synthesize(provider, "Pendente.", "neutral", response_id="old", on_audio=audio))
        await asyncio.wait_for(connected.wait(), 2)
        await queue.cancel("old")
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert provider._socket is None
        assert provider.last_status == "READY"
        assert await queue.synthesize(FakeLocal(tmp_path), "Novo turno.", "neutral", response_id="new")
        await queue.stop()
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("auth,header", [("bearer", "authorization"), ("api_key_header", "x-api-key"), ("custom_header", "x-token"), ("none", None)])
@pytest.mark.parametrize("mode", ["RAW_AUDIO_BYTES", "JSON_BASE64_AUDIO"])
async def test_custom_rest_contract(auth, header, mode, tmp_path):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps({"result": {"audio": base64.b64encode(b"\x01\x00" * 480).decode()}}).encode() if mode == "JSON_BASE64_AUDIO" else b"\x01\x00" * 480
    def handler(request):
        assert json.loads(request.content)["text"] == "Oi."
        if header:
            assert request.headers[header] == ("Bearer " if auth == "bearer" else "") + "fixture-secret"
        else:
            assert "authorization" not in request.headers
        return httpx.Response(200, stream=Body())
    cfg = profile(auth_type=auth, header_name=header if header and auth != "bearer" else "x-api-key",
        response_mode=mode, audio_field="result.audio", streaming=mode == "RAW_AUDIO_BYTES")
    provider = CustomTTSProvider(FakeCredentials(**{cfg.credential_provider: "fixture-secret"}), lambda: True, cfg, transport=httpx.MockTransport(handler))
    data = [p async for p in provider.stream_audio("Oi.")]
    assert sum(len(p.pcm) for p in data) == 960
    assert provider.last_status == "READY"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["WEBSOCKET_BINARY_FRAMES", "WEBSOCKET_JSON_BASE64"])
async def test_custom_real_local_ws(mode):
    observed = []
    async def server(ws):
        observed.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"event": "ready"}))
        observed.append(json.loads(await ws.recv()))
        observed.append(json.loads(await ws.recv()))
        raw = b"\x01\x00" * 480
        await ws.send(raw if mode.endswith("FRAMES") else json.dumps({"event": "sound", "payload": {"pcm": base64.b64encode(raw).decode()}}))
        await ws.send(json.dumps({"event": "done"}))
    async with serve(server, "127.0.0.1", 0) as endpoint:
        cfg = profile(endpoint=f"ws://127.0.0.1:{endpoint.sockets[0].getsockname()[1]}", allow_loopback=True,
            transport="websocket", response_mode=mode, auth_type="none", setup_template={"type": "setup", "rate": "{{sample_rate}}"},
            end_template={"type": "end"}, event_type_field="event", ready_event_value="ready", end_event_value="done",
            audio_event_value="sound", audio_field="payload.pcm")
        provider = CustomTTSProvider(FakeCredentials(), lambda: False, cfg)
        data = [p async for p in provider.stream_audio("Oi.")]
        assert data[0].pcm and observed[0]["rate"] == 48000 and observed[1]["text"] == "Oi."
        await provider.close()
        assert not provider._sockets


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 500, 302])
async def test_custom_failure_sanitized_bounded_and_fallback(tmp_path, caplog, status):
    creds = FakeCredentials(**{"custom:test-provider": "fixture-secret"})
    settings = Settings(tts_provider_id="custom", tts_online_enabled=True,
        tts_universal=UniversalTtsSettings(custom_profiles=[profile()], active_custom_profile="test-provider"))
    local = FakeLocal(tmp_path)
    registry = build_tts_provider_registry(settings, local, creds)
    provider = registry.get("custom")
    provider.transport = httpx.MockTransport(lambda r: httpx.Response(status, content=b"fixture-secret", headers={"location": "https://evil.invalid"}))
    packets = [p async for p in registry.stream_audio("Fallback.")]
    assert packets[0].path and local.calls == ["Fallback."]
    assert "fixture-secret" not in caplog.text
    assert registry.fallback_reason == ("AUTH_ERROR" if status == 401 else "NETWORK_ERROR" if status == 500 else "PROVIDER_ERROR")
    with pytest.raises(TtsProviderError):
        await provider.test_connection()
    await registry.close()


@pytest.mark.asyncio
async def test_runtime_switch_does_not_cancel_active_and_next_speech_uses_selection(tmp_path):
    registry = build_tts_provider_registry(Settings(), FakeLocal(tmp_path), FakeCredentials())
    local = registry.get("local").delegate
    pending, release = asyncio.Event(), asyncio.Event()
    original = local.synthesize
    async def delayed(*args):
        pending.set(); await release.wait(); return await original(*args)
    local.synthesize = delayed
    queue = SpeechQueue()
    active = asyncio.create_task(queue.synthesize(registry, "Primeira.", "neutral", response_id="a"))
    await pending.wait()
    registry.configure(selected_provider="gradium")
    release.set()
    assert await active
    assert await queue.synthesize(registry, "Segunda.", "neutral", response_id="b")
    assert not local.cancelled and local.calls == ["Primeira.", "Segunda."]
    await queue.stop(); await registry.close()


def test_buffered_decoder_and_capabilities():
    assert len(decode_buffered(wav_bytes(), "wav", 24000)) == 480
    for fmt in ("wav", "mp3", "ogg"):
        with pytest.raises(ValueError):
            profile(output_format=fmt, streaming=True)
    caps = GradiumTTSProvider(FakeCredentials(), lambda: False).capabilities().public_dict()
    assert caps["pcm"] and caps["streaming"] and caps["timestamps"]
    assert not caps["emotion"] and not caps["nonverbal"]


@pytest.mark.asyncio
async def test_settings_route_persistence_switch_export_and_secret_rejection(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from app.api import routes
    settings = Settings()
    creds = FakeCredentials(gradium='fixture-secret')
    registry = build_tts_provider_registry(settings, FakeLocal(tmp_path), creds)
    writes = []
    monkeypatch.setattr(routes, "save_runtime_settings", lambda updates: writes.append(updates))
    app = FastAPI()
    # If settings mutation tries to cancel audio, these absent services fail.
    app.state.services = SimpleNamespace(settings=settings, tts_registry=registry)
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        config = UniversalTtsSettings(custom_profiles=[profile()], active_custom_profile="test-provider").model_dump(mode="json")
        response = await client.put('/api/audio/providers/settings', json={"provider": "custom", "universal": config})
        assert response.status_code == 200, response.text
        assert registry.selected_provider == "custom" and writes[-1]["tts_universal"] == config
        exported = await client.get('/api/audio/providers/custom/profiles/test-provider/export')
        assert exported.status_code == 200 and exported.json() == config['custom_profiles'][0]
        assert 'fixture-secret' not in exported.text
        config['custom_profiles'][0]['request_template'] = {"text": "fixture-secret"}
        rejected = await client.put('/api/audio/providers/settings', json={"universal": config})
        assert rejected.status_code == 422 and 'fixture-secret' not in rejected.text
        assert len(writes) == 1
        bad = await client.put('/api/audio/providers/settings', json={"universal": {"token": "private-token"}})
        assert bad.status_code == 422 and 'private-token' not in bad.text
    await registry.close()


@pytest.mark.asyncio
async def test_missing_custom_no_fallback_and_invalid_pcm(tmp_path):
    cfg = profile(fallback="none")
    registry = build_tts_provider_registry(Settings(tts_provider_id="custom",
        tts_universal=UniversalTtsSettings(custom_profiles=[cfg], active_custom_profile=cfg.id)), FakeLocal(tmp_path), FakeCredentials())
    with pytest.raises(TtsProviderError):
        await registry.synthesize("Sem fallback.")
    with pytest.raises(TtsProviderError):
        _ = [p async for p in registry.stream_audio("Sem fallback.")]
    await registry.close()


@pytest.mark.asyncio
async def test_streaming_first_playback_ack_is_not_overwritten(tmp_path):
    from app.api.routes import listening_playback
    from app.listening.models import PlaybackStateRequest
    from unittest.mock import AsyncMock, Mock
    queue = SpeechQueue()
    services = SimpleNamespace(listening=SimpleNamespace(playback=AsyncMock(return_value={})),
        conversation=SimpleNamespace(playback=AsyncMock()), tts_registry=SimpleNamespace(),
        telemetry=SimpleNamespace(playback_started=Mock()), speech_queue=queue,
        event_bus=SimpleNamespace(publish=AsyncMock()))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=services)))
    for delay in (10, 30, 70):
        await listening_playback(PlaybackStateRequest(playing=True, response_id="test-audio", audio_buffer_delay_ms=delay), request)
    services.telemetry.playback_started.assert_called_once_with("test-audio")
    assert services.event_bus.publish.await_count == 1
    assert services.tts_registry.last_audio_buffer_delay_ms == 70


@pytest.mark.asyncio
async def test_custom_cancel_clears_streaming_health():
    ready = asyncio.Event()
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"\x01\x00" * 480
            await asyncio.sleep(30)
    provider = CustomTTSProvider(FakeCredentials(), lambda: True, profile(auth_type="none"),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Body())))
    async def consume():
        async for packet in provider.stream_audio("Cancelamento."):
            ready.set()
    task = asyncio.create_task(consume())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.last_status == "READY" and not provider._active_tasks
