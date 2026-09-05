"""Advanced operator-only declarative TTS contracts; no scripting or generic fetch tool."""
from __future__ import annotations

import asyncio
import base64
import json

import httpx

from app.speech.provider_models import ProviderValidation, TtsProviderError, TtsProviderStatus
from app.speech.provider_transport import (
    MAX_AUDIO_BYTES, MAX_MESSAGE_BYTES, StreamingTtsProvider, decode_buffered,
    open_socket, pcm_packets, pinned_http, receive,
)
from app.speech.synthesis_config import substitute
from app.speech.tts import TtsCapabilities


def field_at(document, path):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class CustomTTSProvider(StreamingTtsProvider):
    def __init__(self, credentials, online_enabled, profile=None, *, connector=open_socket, transport=None):
        super().__init__(credentials, online_enabled, model="", voice="", transport=transport)
        self.profile = profile
        self._connector = connector
        self._sockets = set()

    @property
    def name(self):
        return "custom"

    @property
    def display_name(self):
        return "Outro / Custom"

    @property
    def default_voice(self):
        return self.profile.voice_id if self.profile else ""

    @property
    def configured(self):
        return bool(self.profile and (self.profile.auth_type == "none" or
            self.credentials.has_credential(self.profile.credential_provider)))

    def capabilities(self):
        return TtsCapabilities(supports_streaming=bool(self.profile and self.profile.streaming),
            voice_selection=True, model_selection=True, pcm=True,
            offline=bool(self.profile and self.profile.allow_loopback))

    def configuration(self):
        if not self.configured:
            return ProviderValidation(False, TtsProviderStatus.NOT_CONFIGURED, "profile_or_credential_missing")
        if not self.online_enabled() and not self.profile.allow_loopback:
            return ProviderValidation(False, TtsProviderStatus.DISABLED, "online_disabled")
        return ProviderValidation(True, TtsProviderStatus.READY)

    def context(self, profile, text, state, options):
        return {"text": text, "voice_id": profile.voice_id, "model": profile.model,
            "language": profile.language, "sample_rate": profile.sample_rate,
            "output_format": profile.output_format, "emotion": getattr(options, "emotion", state),
            "style": getattr(options, "style_instruction", ""), "speed": getattr(options, "speaking_rate", 1.)}

    def headers(self, profile):
        if profile.auth_type == "none":
            return {}
        secret = self.credentials.get_for_authorized_provider(profile.credential_provider)
        if not secret or "\r" in secret or "\n" in secret:
            raise TtsProviderError(TtsProviderStatus.NOT_CONFIGURED, "Credencial Custom ausente ou inválida.")
        if profile.auth_type == "bearer":
            return {"Authorization": f"Bearer {secret}"}
        return {profile.header_name: secret}

    async def _rest(self, profile, context, headers):
        if self.transport is not None:  # In-process tests only; never persisted or exposed in API.
            url, host, extensions = profile.endpoint, {}, {}
        else:
            url, host, extensions = await pinned_http(profile.endpoint, profile.allow_loopback)
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False, transport=self.transport) as client:
            async with client.stream("POST", url, headers={**host, **headers}, extensions=extensions,
                    json=substitute(profile.request_template, context)) as response:
                if response.status_code != 200:
                    raise self._remote_failure(response)
                total = 0
                buffered = bytearray()
                # iter_raw preserves incremental delivery, avoids waiting for a
                # requested chunk_size and disallows compressed HTTP bombs.
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise ValueError("HTTP compression não suportada para áudio.")
                async for data in response.aiter_raw():
                    total += len(data)
                    if total > MAX_AUDIO_BYTES * 2:
                        raise ValueError("Resposta excedeu o limite.")
                    if profile.response_mode == "JSON_BASE64_AUDIO":
                        buffered.extend(data)
                    else:
                        yield data
                if profile.response_mode == "JSON_BASE64_AUDIO":
                    yield base64.b64decode(field_at(json.loads(buffered), profile.audio_field), validate=True)

    async def _websocket(self, profile, context, headers):
        ws = await self._connector(profile.endpoint, headers, profile.allow_loopback)
        self._sockets.add(ws)
        try:
            if profile.setup_template is not None:
                await ws.send(json.dumps(substitute(profile.setup_template, context)))
            if profile.ready_event_value:
                ready = json.loads(await receive(ws))
                if field_at(ready, profile.event_type_field) != profile.ready_event_value:
                    raise ValueError("Ready não confirmado.")
            await ws.send(json.dumps(substitute(profile.text_template, context)))
            if profile.end_template is not None:
                await ws.send(json.dumps(substitute(profile.end_template, context)))
            while True:
                message = await receive(ws)
                if isinstance(message, bytes):
                    if profile.response_mode != "WEBSOCKET_BINARY_FRAMES":
                        raise ValueError("Frame inesperado.")
                    yield message
                    continue
                document = json.loads(message)
                event = field_at(document, profile.event_type_field)
                if profile.end_event_value and event == profile.end_event_value:
                    return
                if event == "error":
                    raise TtsProviderError(TtsProviderStatus.ERROR, "Provider Custom recusou a síntese.")
                if profile.response_mode == "WEBSOCKET_JSON_BASE64" and (not profile.audio_event_value or event == profile.audio_event_value):
                    yield base64.b64decode(field_at(document, profile.audio_field), validate=True)
        except (asyncio.CancelledError, GeneratorExit):
            if profile.cancel_template is not None:
                try:
                    await asyncio.wait_for(ws.send(json.dumps(substitute(profile.cancel_template, context))), 1)
                except Exception:
                    pass
            raise
        finally:
            self._sockets.discard(ws)
            await ws.close()

    async def stream_audio(self, text, state="neutral", options=None):
        self.check_ready()
        profile = self.profile  # immutable request snapshot, including templates/auth identity
        if not text.strip() or len(text) > 12000:
            raise TtsProviderError(TtsProviderStatus.ERROR, "Texto TTS inválido.")
        context = self.context(profile, text, state, options)
        headers = self.headers(profile)
        task = self._begin_request()
        started = self.begin_timing()
        self.last_status = TtsProviderStatus.CONNECTING
        source = self._rest(profile, context, headers) if profile.transport == "rest" else self._websocket(profile, context, headers)
        try:
            pending = bytearray()
            total = 0
            async with asyncio.timeout(180):
                async for data in source:
                    total += len(data)
                    if total > MAX_AUDIO_BYTES:
                        raise ValueError("Áudio excessivo.")
                    pending.extend(data)
                    if profile.streaming:
                        size = len(pending) // 2 * 2
                        if size:
                            self.first_audio(started)
                            for packet in pcm_packets(bytes(pending[:size]), profile.sample_rate):
                                yield packet
                            del pending[:size]
                if not total or (profile.streaming and pending):
                    raise ValueError("Áudio vazio ou desalinhado.")
                if not profile.streaming:
                    pcm = await asyncio.to_thread(decode_buffered, bytes(pending), profile.output_format, profile.sample_rate)
                    self.first_audio(started)
                    for packet in pcm_packets(pcm, profile.sample_rate):
                        yield packet
                self.mark("total_synthesis_ms", started)
                self.succeeded()
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception as exc:
            raise self.failed(exc) from None
        finally:
            await source.aclose()
            self._finish_request(task)
            if self.last_status in {TtsProviderStatus.CONNECTING, TtsProviderStatus.STREAMING}:
                self.last_status = self.configuration().status

    async def test_connection(self):
        # Arbitrary REST APIs have no universal health endpoint. Validate one
        # minimal synthesis and discard audio; do not claim HEAD implies TTS works.
        total = 0
        async for packet in self.stream_audio("Oi."):
            total += len(packet.pcm)
        return {"status": "READY", "audio_validated": bool(total), "playback": False}

    async def cancel(self):
        await super().cancel()
        for ws in tuple(self._sockets):
            await ws.close()
        self._sockets.clear()
