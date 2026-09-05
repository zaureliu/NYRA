from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

from app.core.runtime_settings import save_runtime_settings

from .credentials import STTCredentialBroker
from .deepgram import DeepgramSTTProvider
from .local import FasterWhisperSTTProvider
from .models import (AudioFormat, CanonicalTranscript, RecognitionEvent, STTFailure,
                     STTSettings, STTState)


class STTProviderRegistry:
    """One capture lease, one STT session, one broker, bounded replay on failure."""

    def __init__(self, settings, local_engine, credentials: STTCredentialBroker, event_bus, *, persist=save_runtime_settings):
        self.settings = settings
        self.local_engine = local_engine
        self.credentials = credentials
        self.event_bus = event_bus
        self.persist = persist
        self.config = settings.stt_recognition
        self.active: RecognitionSession | None = None
        self.lock = asyncio.Lock()
        self.available = asyncio.Event()
        self.available.set()
        self.tickets: dict[str, tuple[float, dict]] = {}
        self.deepgram_state = STTState.READY if credentials.configured() else STTState.NOT_CONFIGURED
        self.last_failure: str | None = None
        self.failures = 0
        self.retry_at = 0.0
        self.last_diagnostics: dict = {}
        self.closed = False

    @property
    def name(self):
        return self.active.provider.provider_id if self.active and self.active.provider else self.config.provider

    @property
    def model_name(self):
        return self.config.model if self.config.provider == "deepgram" else self.local_engine.model_name

    @property
    def loaded(self):
        return self.local_engine.loaded

    async def health(self) -> bool:
        return await self.local_engine.health()

    async def transcribe(self, path):
        # Compatibility for prerecorded uploads/lab tools. Live microphones use
        # /stt/stream; never present this file route as realtime cloud streaming.
        return await self.local_engine.transcribe(path)

    def get_dynamic_keyterms(self, context: dict | None = None) -> list[str]:
        """Extension point. No context/memory is transmitted by this V1."""
        return []

    def issue_ticket(self, request: dict) -> str:
        now = time.monotonic()
        self.tickets = {key: value for key, value in self.tickets.items() if value[0] > now}
        if len(self.tickets) >= 8 or self.closed:
            raise STTFailure(STTState.ERROR, "STT ticket limit reached")
        token = secrets.token_urlsafe(32)
        self.tickets[token] = (now + 15, request)
        return token

    def consume_ticket(self, token: str) -> dict:
        entry = self.tickets.pop(token, None)
        if not entry or entry[0] <= time.monotonic():
            raise STTFailure(STTState.ERROR, "STT ticket expired or invalid")
        return entry[1]

    async def open_session(self, audio_format: AudioFormat, sink, *, mic_started_at: float | None = None):
        async with self.lock:
            if self.closed or self.active is not None:
                raise STTFailure(STTState.ERROR, "Microphone recognition already in use")
            session = RecognitionSession(self, audio_format, sink, mic_started_at=mic_started_at)
            self.active = session
            self.available.clear()
            session.worker = asyncio.create_task(session.run(), name="nyra-stt-audio-sender")
            return session

    def remote_failed(self, failure: STTFailure) -> None:
        self.deepgram_state = failure.state
        self.last_failure = failure.code
        self.failures = min(self.failures + 1, 5)
        # Reconnect on a subsequent utterance, never in a tight background loop.
        self.retry_at = time.monotonic() + min(60, 5 * 2 ** (self.failures - 1))
        if failure.state in (STTState.AUTH_ERROR, STTState.ERROR):
            self.retry_at = float("inf")  # reset by credential/config update

    async def update(self, config: STTSettings) -> dict:
        async with self.lock:
            await asyncio.to_thread(self.persist, {"stt_recognition": config.model_dump(mode="json")})
            self.config = config
            self.settings.stt_recognition = config
            self.retry_at = 0
            self.failures = 0
            self.last_failure = None
            if self.active:
                await self.active.force_fallback("STT settings changed")
            self.deepgram_state = STTState.READY if self.credentials.configured() else STTState.NOT_CONFIGURED
        return await self.status()

    async def credential_changed(self) -> None:
        self.retry_at = 0
        self.failures = 0
        self.last_failure = None
        if self.active:
            await self.active.force_fallback("Deepgram credential changed")
        self.deepgram_state = STTState.READY if self.credentials.configured() else STTState.NOT_CONFIGURED

    async def status(self) -> dict:
        configured = self.credentials.configured()
        local_available = await self.local_engine.health()
        session = self.active
        active_provider = session.provider.provider_id if session and session.provider else None
        return {
            "settings": self.config.model_dump(mode="json"),
            "credential_configured": configured,
            "deepgram_state": self.deepgram_state.value if configured else STTState.NOT_CONFIGURED.value,
            "active_provider": active_provider,
            "connection_state": session.state.value if session else self.deepgram_state.value if self.config.provider == "deepgram" else "READY" if local_available else "ERROR",
            "fallback_available": local_available, "fallback_loaded": self.local_engine.loaded,
            "fallback_active": bool(session and session.fallback_reason),
            "last_error": self.last_failure,
            "diagnostics": session.diagnostics() if session else self.last_diagnostics,
            "providers": [
                {"id": "deepgram", "capabilities": DeepgramSTTProvider.capabilities(None).model_dump()},
                {"id": "faster_whisper", "capabilities": FasterWhisperSTTProvider.capabilities(None).model_dump(),
                 "model": self.local_engine.model_name},
            ],
        }

    async def close(self) -> None:
        self.closed = True
        self.tickets.clear()
        if self.active:
            await self.active.close()


class RecognitionSession:
    MAX_SECONDS = 60
    MAX_CHUNK_BYTES = 32768

    def __init__(self, registry: STTProviderRegistry, audio_format: AudioFormat,
                 sink: Callable[[dict], Awaitable[None]], *, mic_started_at=None):
        self.registry = registry
        self.config = registry.config.model_copy(deep=True)
        self.audio_format = audio_format
        self.sink = sink
        self.utterance_id = "utt_" + uuid4().hex
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=32)
        self.audio = bytearray()
        self.provider = None
        self.worker: asyncio.Task | None = None
        self.operation_lock = asyncio.Lock()
        self.state = STTState.CONNECTING
        self.fallback_reason: str | None = None
        self.force_local = False
        self.closed = False
        self.ended = False
        self.final: CanonicalTranscript | None = None
        self.started = time.time()
        self.mic_started = mic_started_at if mic_started_at and 0 <= self.started - mic_started_at < 60 else self.started
        self.marks: dict[str, float] = {}
        self.queue_overflows = 0
        self.audio_sent_bytes = 0
        self.duplicates = 0

    async def emit(self, event: RecognitionEvent) -> None:
        key = {"interim": "first_interim_received", "final": "first_final_received"}.get(event.type, event.type)
        self.marks.setdefault(key, time.time())
        if event.type == "state" and event.state in (STTState.NETWORK_ERROR, STTState.AUTH_ERROR, STTState.ERROR):
            self.registry.deepgram_state = event.state
            self.state = STTState.DEGRADED
        # Stream events go only to the authenticated local stream consumer.
        # They are not persisted in the EventBus history / Memory V2.
        await self.sink({"type": "stt_event", "utterance_id": self.utterance_id,
                         **event.model_dump(mode="json")})

    async def run(self) -> None:
        try:
            async with self.operation_lock:
                if self.force_local:
                    pass  # a settings/credential change already installed fallback
                elif self.config.provider == "deepgram" and self.registry.credentials.configured() and time.monotonic() >= self.registry.retry_at:
                    self.provider = DeepgramSTTProvider(self.config, self.audio_format, self.registry.credentials,
                                                         self.utterance_id, self.emit,
                                                         keyterms=self.registry.get_dynamic_keyterms())
                    self.registry.deepgram_state = STTState.CONNECTING
                    try:
                        await self.provider.start_stream()
                        self.registry.deepgram_state = STTState.STREAMING
                        self.registry.last_failure = None
                        self.state = STTState.STREAMING
                    except STTFailure as failure:
                        self.registry.remote_failed(failure)
                        await self._fallback(failure.code)
                else:
                    await self._fallback("Deepgram not configured or reconnect cooldown" if self.config.provider == "deepgram" else None)
            while True:
                audio = await self.queue.get()
                try:
                    if audio is None:
                        break
                    async with self.operation_lock:
                        if self.provider and self.provider.provider_id == "deepgram":
                            try:
                                await self.provider.send_audio(audio)
                                self.marks.setdefault("audio_first_chunk_sent", time.time())
                                self.audio_sent_bytes += len(audio)
                            except STTFailure as failure:
                                self.registry.remote_failed(failure)
                                await self._fallback(failure.code)
                finally:
                    self.queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.state = STTState.ERROR
            raise STTFailure(STTState.ERROR, "Recognition stream failed") from None

    async def _fallback(self, reason: str | None):
        if self.provider:
            self.duplicates += getattr(getattr(self.provider, "assembly", None), "duplicates", 0)
            await self.provider.close()
        self.provider = FasterWhisperSTTProvider(self.registry.local_engine, self.audio_format, self.utterance_id, self.emit)
        await self.provider.connect()
        self.fallback_reason = reason
        self.state = STTState.FALLBACK if reason else STTState.READY
        await self.emit(RecognitionEvent(type="state", state=self.state))

    async def force_fallback(self, reason: str):
        self.force_local = True
        async with self.operation_lock:
            if not self.closed and self.final is None:
                await self._fallback(reason)

    async def send_audio(self, audio: bytes) -> None:
        if self.closed or self.ended:
            raise STTFailure(STTState.ERROR, "Recognition stream already ended")
        if not audio or len(audio) > self.MAX_CHUNK_BYTES or len(audio) % 2:
            raise STTFailure(STTState.ERROR, "Invalid PCM audio frame")
        if len(self.audio) + len(audio) > self.audio_format.bytes_per_second * self.MAX_SECONDS:
            raise STTFailure(STTState.ERROR, "Audio duration limit exceeded")
        self.marks.setdefault("mic_first_chunk_received", time.time())
        self.audio.extend(audio)
        try:
            self.queue.put_nowait(audio)
        except asyncio.QueueFull:
            self.queue_overflows += 1
            # The complete bounded replay buffer owns the sample. Never send a
            # gapped remote transcript: invalidate remote and replay once locally.
            await self.force_fallback("Audio backpressure; local replay")
            while not self.queue.empty():
                self.queue.get_nowait()
                self.queue.task_done()

    async def finish(self) -> CanonicalTranscript:
        if self.final:
            return self.final
        if self.closed or self.ended:
            raise STTFailure(STTState.ERROR, "Recognition stream already ended")
        self.ended = True
        await asyncio.wait_for(self.queue.put(None), 6)
        await asyncio.wait_for(asyncio.shield(self.worker), 10)
        async with self.operation_lock:
            if self.provider.provider_id == "deepgram":
                try:
                    self.final = await self.provider.finish()
                    self.duplicates += self.provider.assembly.duplicates
                    self.registry.failures = 0
                    self.registry.retry_at = 0
                except STTFailure as failure:
                    self.registry.remote_failed(failure)
                    await self._fallback(failure.code)
            if self.final is None:
                await self.provider.send_audio(bytes(self.audio))
                self.final = await self.provider.finish()
        if self.final.text:
            self.marks["last_final_transcript_time"] = time.time()
        return self.final

    def diagnostics(self) -> dict:
        def latency(key):
            return round((self.marks[key] - self.mic_started) * 1000, 1) if key in self.marks else None
        return {
            "utterance_id": self.utterance_id, "audio_format": self.audio_format.model_dump(),
            "audio_duration_seconds": round(len(self.audio) / self.audio_format.bytes_per_second, 3),
            "audio_sent_bytes": self.audio_sent_bytes, "queue_limit": self.queue.maxsize,
            "queue_overflows": self.queue_overflows, "duplicates_suppressed": self.duplicates,
            "mic_to_first_interim_ms": latency("first_interim_received"),
            "mic_to_final_ms": latency("first_final_received"), "timestamps": dict(self.marks),
            "fallback_reason": self.fallback_reason,
            "final_provider": self.final.provider if self.final else None,
        }

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.worker and not self.worker.done():
            self.worker.cancel()
        if self.worker:
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.provider:
            await self.provider.close()
        self.registry.last_diagnostics = self.diagnostics()
        self.audio.clear()
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        if self.registry.active is self:
            self.registry.active = None
            self.registry.available.set()
        if self.registry.deepgram_state == STTState.STREAMING:
            self.registry.deepgram_state = STTState.READY
