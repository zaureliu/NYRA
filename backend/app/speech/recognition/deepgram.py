from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from urllib.parse import urlencode

from websockets.asyncio.client import connect

from .assembly import TranscriptAssembly
from .credentials import STTCredentialBroker
from .models import (AudioFormat, CanonicalTranscript, EventSink, RecognitionEvent,
                     RealtimeSTTProvider, STTCapabilities, STTFailure, STTSettings,
                     STTState, TranscriptWord)


# websockets DEBUG logs include handshake headers. This private logger must
# never propagate, even when the operator enables global diagnostic logging.
_transport_logger = logging.Logger("kazumi.deepgram.private", level=logging.CRITICAL + 1)
_transport_logger.addHandler(logging.NullHandler())
_transport_logger.propagate = False


def stream_options(settings: STTSettings, audio_format: AudioFormat, keyterms=()) -> list[tuple[str, str]]:
    values = {
        "model": settings.model, "language": settings.language,
        "smart_format": settings.smart_format, "interim_results": settings.interim_results,
        "endpointing": settings.endpointing, "vad_events": settings.vad_events,
        "punctuate": settings.punctuate, "numerals": settings.numerals,
        "profanity_filter": settings.profanity_filter, "diarize": settings.diarize,
        "dictation": settings.dictation, **audio_format.model_dump(),
    }
    # Redact is a list of entity types, not a boolean in the listen API. Off is
    # represented by omitting it. UtteranceEnd requires interim_results.
    if settings.interim_results:
        values["utterance_end_ms"] = settings.utterance_end_ms
    result = [(key, str(value).lower() if isinstance(value, bool) else str(value)) for key, value in values.items()]
    if settings.keyterms_enabled:
        terms = STTSettings(keyterms=list(dict.fromkeys([*settings.keyterms, *keyterms]))[:20]).keyterms
        result.extend(("keyterm", term) for term in terms)
    return result


def connection_failure(exc: Exception) -> STTFailure:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return STTFailure(STTState.AUTH_ERROR, "Deepgram authentication failed")
    if status in (400, 404, 422):
        return STTFailure(STTState.ERROR, "Deepgram configuration rejected")
    return STTFailure(STTState.NETWORK_ERROR, "Deepgram connection unavailable")


class DeepgramSTTProvider(RealtimeSTTProvider):
    provider_id = "deepgram"

    def __init__(self, settings: STTSettings, audio_format: AudioFormat,
                 credentials: STTCredentialBroker, utterance_id: str, sink: EventSink,
                 *, connector=connect, keyterms=()):
        self.settings = settings
        self.audio_format = audio_format
        self.credentials = credentials
        self.utterance_id = utterance_id
        self.sink = sink
        self.connector = connector
        self.keyterms = keyterms
        self.state = STTState.NOT_CONFIGURED if not credentials.configured() else STTState.READY
        self.socket = None
        self.receiver: asyncio.Task | None = None
        self.keepalive: asyncio.Task | None = None
        self.failure: STTFailure | None = None
        self.assembly = TranscriptAssembly()
        self.sequence = 0
        self.audio_seconds = 0.0
        self.last_sent = time.monotonic()
        self.finishing = False
        self.closed = False
        self.received_metadata = False
        self.provider_model: str | None = None

    def capabilities(self) -> STTCapabilities:
        return STTCapabilities(streaming=True, interim_results=True, speech_started=True,
                               endpointing=True, utterance_end=True, remote=True, word_timestamps=True)

    async def connect(self) -> None:
        if self.socket:
            return
        secret = self.credentials.resolve()
        if not secret:
            raise STTFailure(STTState.NOT_CONFIGURED, "Deepgram credential not configured")
        self.state = STTState.CONNECTING
        try:
            self.socket = await self.connector(
                "wss://api.deepgram.com/v1/listen?" + urlencode(stream_options(self.settings, self.audio_format, self.keyterms)),
                additional_headers={"Authorization": f"Token {secret}"},
                open_timeout=5, close_timeout=2, ping_interval=20, ping_timeout=10,
                max_size=1024 * 1024, max_queue=16, logger=_transport_logger,
            )
        except Exception as exc:
            failure = connection_failure(exc)
            self.state = failure.state
            raise failure from None
        finally:
            secret = None
        self.state = STTState.STREAMING
        self.receiver = asyncio.create_task(self._receive(), name="kazumi-stt-deepgram-receiver")
        self.keepalive = asyncio.create_task(self._keepalive(), name="kazumi-stt-deepgram-keepalive")

    async def _send(self, data: bytes | str) -> None:
        if self.failure:
            raise self.failure
        if not self.socket or self.closed:
            raise STTFailure(STTState.NETWORK_ERROR, "Deepgram stream closed")
        try:
            await asyncio.wait_for(self.socket.send(data), timeout=2)
            self.last_sent = time.monotonic()
        except Exception as exc:
            raise connection_failure(exc) from None

    async def send_audio(self, audio: bytes) -> None:
        await self._send(audio)
        self.audio_seconds += len(audio) / self.audio_format.bytes_per_second

    async def _keepalive(self) -> None:
        try:
            while True:
                await asyncio.sleep(3)
                if time.monotonic() - self.last_sent >= 3:
                    await self._send('{"type":"KeepAlive"}')
        except STTFailure as failure:
            await self._failed(failure)

    async def _failed(self, failure: STTFailure) -> None:
        self.failure = failure
        self.state = failure.state
        await self.sink(RecognitionEvent(type="state", state=self.state))

    async def _receive(self) -> None:
        try:
            async for raw in self.socket:
                await self.parse_message(json.loads(raw))
            if not self.finishing and not self.closed:
                await self._failed(STTFailure(STTState.NETWORK_ERROR, "Deepgram disconnected"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.closed:
                await self._failed(connection_failure(exc))

    async def parse_message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "Error":
            await self._failed(STTFailure(STTState.ERROR, "Deepgram provider error"))
            return
        if kind == "Metadata":
            self.received_metadata = True
            return
        if kind in ("SpeechStarted", "UtteranceEnd"):
            await self.sink(RecognitionEvent(
                type="speech_started" if kind == "SpeechStarted" else "utterance_end",
                timestamp=message.get("timestamp", message.get("last_word_end")),
            ))
            return
        if kind != "Results":
            return
        alternatives = (message.get("channel") or {}).get("alternatives") or []
        if not alternatives:
            return
        alternative = alternatives[0]
        is_final = message.get("is_final") is True
        speech_final = message.get("speech_final") is True
        self.sequence += 1
        started = float(message.get("start", 0))
        transcript = CanonicalTranscript(
            text=str(alternative.get("transcript") or ""), is_final=is_final,
            speech_final=speech_final, confidence=alternative.get("confidence"),
            started_at=started, ended_at=started + float(message.get("duration", 0)),
            provider=self.provider_id, language=self.settings.language,
            utterance_id=self.utterance_id, sequence=self.sequence,
            words=[TranscriptWord(text=str(word.get("punctuated_word") or word.get("word") or ""),
                                  started_at=float(word.get("start", 0)), ended_at=float(word.get("end", 0)),
                                  confidence=word.get("confidence")) for word in alternative.get("words", [])],
        )
        if is_final:
            if self.assembly.add(transcript):
                await self.sink(RecognitionEvent(type="final", transcript=transcript))
        elif transcript.text:
            await self.sink(RecognitionEvent(type="interim", transcript=transcript))
        if speech_final:
            await self.sink(RecognitionEvent(type="speech_final", timestamp=transcript.ended_at))

    async def finish(self) -> CanonicalTranscript:
        self.finishing = True
        # CloseStream flushes unfinalized audio and produces final Results and
        # Metadata before the server closes. is_final alone never submits chat.
        await self._send('{"type":"CloseStream"}')
        try:
            await asyncio.wait_for(asyncio.shield(self.receiver), timeout=5)
        except TimeoutError:
            raise STTFailure(STTState.NETWORK_ERROR, "Deepgram finalization timed out") from None
        if self.failure:
            raise self.failure
        if not self.received_metadata:
            raise STTFailure(STTState.NETWORK_ERROR, "Deepgram finalization incomplete")
        self.sequence += 1
        return self.assembly.finish(CanonicalTranscript(
            text="", is_final=True, provider=self.provider_id, language=self.settings.language,
            utterance_id=self.utterance_id, sequence=self.sequence, ended_at=self.audio_seconds,
        ))

    async def close(self) -> None:
        self.closed = True
        for task in (self.receiver, self.keepalive):
            if task:
                task.cancel()
        if self.socket:
            with suppress(Exception):
                await asyncio.wait_for(self.socket.close(), 3)
            self.socket = None
        await asyncio.gather(*(task for task in (self.receiver, self.keepalive) if task), return_exceptions=True)
        self.receiver = self.keepalive = None
        if self.state == STTState.STREAMING:
            self.state = STTState.READY
