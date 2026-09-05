"""Native Gradium WebSocket TTS. Protocol verified 2026-09-05 in official docs."""
from __future__ import annotations

import asyncio
import base64
import json
from collections import deque
from uuid import uuid4

import httpx
from websockets.protocol import State

from app.speech.provider_models import ProviderValidation, TtsProviderError, TtsProviderStatus
from app.speech.provider_transport import MAX_AUDIO_BYTES, StreamingTtsProvider, open_socket, pcm_packets, receive
from app.speech.stream import AudioPacket, SpeechTimestamp
from app.speech.synthesis_config import GradiumSettings
from app.speech.tts import TtsCapabilities


class GradiumTTSProvider(StreamingTtsProvider):
    def __init__(self, credentials, online_enabled, settings=None, *, connector=open_socket):
        self.settings = settings or GradiumSettings()
        super().__init__(credentials, online_enabled, model=self.settings.model, voice=self.settings.voice_id)
        self._connector = connector
        self._socket = None
        self._socket_key = None
        self._lock = asyncio.Lock()
        self._voices = []
        self.alignment = deque(maxlen=200)

    @property
    def name(self):
        return "gradium"

    @property
    def display_name(self):
        return "Gradium"

    def capabilities(self):
        return TtsCapabilities(supports_streaming=True, timestamps=True, voice_selection=True,
            model_selection=True, pcm=True, offline=False, supports_native_speed=True)

    def configuration(self):
        if not self.configured:
            return ProviderValidation(False, TtsProviderStatus.NOT_CONFIGURED, "credential_missing")
        common = self._common_validation()
        if common:
            return common
        if not self.voice:
            return ProviderValidation(False, TtsProviderStatus.NOT_CONFIGURED, "voice_id_missing")
        return ProviderValidation(True, TtsProviderStatus.READY)

    async def _connect(self, cfg):
        secret = self.credentials.get_for_authorized_provider("gradium")
        if not secret:
            raise TtsProviderError(TtsProviderStatus.NOT_CONFIGURED, "Credencial ausente.")
        # Secret remains backend-only, never part of settings, URL or diagnostics.
        key = (cfg.endpoint, secret)
        if self._socket is not None and (self._socket_key != key or self._socket.state != State.OPEN):
            await self._close_socket()
        if self._socket is None:
            self._socket = await self._connector(cfg.endpoint, {"x-api-key": secret})
            self._socket_key = key
        return self._socket

    def setup_message(self, request_id, cfg):
        result = {"type": "setup", "model_name": cfg.model, "voice_id": cfg.voice_id,
            "output_format": "pcm" if cfg.sample_rate == 48000 else f"pcm_{cfg.sample_rate}",
            "json_config": cfg.json_config.model_dump(exclude_none=True),
            "close_ws_on_eos": False, "client_req_id": request_id}
        if cfg.pronunciation_id:
            result["pronunciation_id"] = cfg.pronunciation_id
        return result

    async def stream_audio(self, text, state="neutral", options=None):
        # SpeechPlanner already yields sentence-sized LLM chunks. Each request
        # shares the session socket without buffering a whole response/WAV.
        async def chunks():
            yield text
        async for packet in self.stream_text(chunks(), state, options):
            yield packet

    async def stream_text(self, texts, state="neutral", options=None):
        self.check_ready()
        cfg = self.settings.model_copy(update={"voice_id": self.voice, "model": self.model_id})
        sender = None
        async with self._lock:
            task = self._begin_request()
            started = self.begin_timing()
            self.alignment.clear()
            self.last_status = TtsProviderStatus.CONNECTING
            try:
                async with asyncio.timeout(180):
                    ws = await self._connect(cfg)
                    req = uuid4().hex
                    await ws.send(json.dumps(self.setup_message(req, cfg)))
                    ready = json.loads(await receive(ws, self.timeout_seconds))
                    if ready.get("type") != "ready" or ready.get("client_req_id") != req or ready.get("sample_rate") != cfg.sample_rate:
                        raise ValueError("Setup Gradium inválido.")
                    self.mark("connection_ready_ms", started)
                    async def send():
                        total_chars = 0
                        async for part in texts:
                            total_chars += len(part)
                            if total_chars > 12000:
                                raise ValueError("Texto excedeu o limite.")
                            if part.strip():
                                if "text_sent_ms" not in self.last_metrics:
                                    self.mark("text_sent_ms", started)
                                await ws.send(json.dumps({"type": "text", "text": part, "client_req_id": req}))
                        await ws.send(json.dumps({"type": "end_of_stream", "client_req_id": req}))
                    sender = asyncio.create_task(send(), name="gradium-text-sender")
                    total = 0
                    while True:
                        if sender.done() and sender.exception():
                            raise sender.exception()
                        msg = json.loads(await receive(ws, self.timeout_seconds))
                        if msg.get("client_req_id") != req:
                            raise ValueError("Mensagem Gradium de outra requisição.")
                        kind = msg.get("type")
                        if kind == "audio":
                            data = base64.b64decode(msg["audio"], validate=True)
                            total += len(data)
                            if total > min(MAX_AUDIO_BYTES, cfg.sample_rate * 2 * 180):
                                raise ValueError("Áudio excessivo.")
                            if data:
                                self.first_audio(started)
                                for packet in pcm_packets(data, cfg.sample_rate):
                                    yield packet
                        elif kind == "text":
                            begin, end = float(msg["start_s"]), float(msg["stop_s"])
                            if not 0 <= begin <= end <= 180:
                                raise ValueError("Timestamp inválido.")
                            timestamp = SpeechTimestamp(str(msg["text"])[:2000], begin, end)
                            self.alignment.append(timestamp)
                            yield AudioPacket(sample_rate=cfg.sample_rate, timestamps=(timestamp,))
                        elif kind == "error":
                            raise TtsProviderError(TtsProviderStatus.ERROR, "Gradium recusou a síntese.")
                        elif kind == "end_of_stream":
                            await sender
                            if not total:
                                raise ValueError("Sem áudio.")
                            self.mark("total_synthesis_ms", started)
                            self.succeeded()
                            break
            except (asyncio.CancelledError, GeneratorExit):
                # No documented request-abort message. Close on barge-in, do not
                # pretend EOS cancels generation or reuse stale buffered audio.
                await self._close_socket()
                raise
            except Exception as exc:
                await self._close_socket()
                raise self.failed(exc) from None
            finally:
                if sender:
                    if not sender.done():
                        sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                self._finish_request(task)
                if self.last_status in {TtsProviderStatus.CONNECTING, TtsProviderStatus.STREAMING}:
                    self.last_status = self.configuration().status

    async def _close_socket(self):
        ws, self._socket = self._socket, None
        self._socket_key = None
        if ws:
            await ws.close()

    async def cancel(self):
        await super().cancel()
        await self._close_socket()

    async def close(self):
        await self.cancel()

    async def test_connection(self):
        self.check_ready()
        async with self._lock:
            try:
                ws = await self._connect(self.settings)
                request_id = uuid4().hex
                await ws.send(json.dumps(self.setup_message(request_id, self.settings)))
                ready = json.loads(await receive(ws, 15))
                if ready.get("type") != "ready" or ready.get("sample_rate") != self.settings.sample_rate:
                    raise ValueError("Setup não confirmado.")
                self.succeeded()
                return {"status": "READY", "authenticated": True, "websocket": True, "sample_rate": ready["sample_rate"]}
            except Exception as exc:
                raise self.failed(exc) from None
            finally:
                await self._close_socket()

    async def list_voices(self, refresh=False):
        if refresh:
            if not self.online_enabled() or not self.configured:
                raise TtsProviderError(TtsProviderStatus.NOT_CONFIGURED, "Configure a credencial e habilite providers online.")
            endpoint = self.settings.endpoint.replace("wss://", "https://").replace("/speech/tts", "/voices/")
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
                    response = await client.get(endpoint, params={"include_catalog": "true", "limit": 200},
                        headers={"x-api-key": self.credentials.get_for_authorized_provider("gradium")})
                    if response.status_code != 200:
                        raise self._remote_failure(response)
                    if len(response.content) > 1024 * 1024:
                        raise ValueError("Catálogo excessivo.")
                    self._voices = [{"id": str(item["uid"])[:128], "name": str(item["name"])[:128],
                        "language": str(item.get("language") or "")[:32]} for item in response.json()[:200]]
            except Exception as exc:
                raise self.failed(exc) from None
        return list(self._voices)
