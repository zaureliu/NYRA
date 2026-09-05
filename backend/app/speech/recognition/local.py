from __future__ import annotations

import asyncio

from .models import (AudioFormat, CanonicalTranscript, EventSink, RecognitionEvent,
                     RealtimeSTTProvider, STTCapabilities, STTFailure, STTState)


class FasterWhisperSTTProvider(RealtimeSTTProvider):
    """Buffered adapter over NYRA's existing model and Silero. No streaming claim."""

    provider_id = "faster_whisper"

    def __init__(self, engine, audio_format: AudioFormat, utterance_id: str, sink: EventSink):
        self.engine = engine
        self.audio_format = audio_format
        self.utterance_id = utterance_id
        self.sink = sink
        self.buffer = bytearray()
        self.state = STTState.READY
        self.worker: asyncio.Task | None = None

    def capabilities(self) -> STTCapabilities:
        return STTCapabilities()

    async def connect(self) -> None:
        if not await self.engine.health():
            raise STTFailure(STTState.ERROR, "Faster-Whisper unavailable")

    async def send_audio(self, audio: bytes) -> None:
        if len(self.buffer) + len(audio) > self.audio_format.bytes_per_second * 60:
            raise STTFailure(STTState.ERROR, "Audio duration limit exceeded")
        self.buffer.extend(audio)

    async def finish(self) -> CanonicalTranscript:
        if not self.buffer:
            return CanonicalTranscript(text="", is_final=True, provider=self.provider_id,
                                       language=self.engine.language, utterance_id=self.utterance_id, sequence=1)
        self.worker = asyncio.create_task(self.engine.transcribe_pcm(bytes(self.buffer), self.audio_format.sample_rate),
                                          name="nyra-stt-local-worker")
        try:
            result = await asyncio.shield(self.worker)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise STTFailure(STTState.ERROR, "Faster-Whisper transcription failed") from None
        transcript = CanonicalTranscript(
            text=result.text, is_final=True, speech_final=True, provider=self.provider_id,
            language=result.language, utterance_id=self.utterance_id, sequence=1,
            ended_at=len(self.buffer) / self.audio_format.bytes_per_second,
        )
        await self.sink(RecognitionEvent(type="final", transcript=transcript))
        return transcript

    async def close(self) -> None:
        self.buffer.clear()
        # A native CTranslate2 call cannot safely be killed mid-inference.
        # Join the existing bounded utterance worker rather than orphan it.
        if self.worker:
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
