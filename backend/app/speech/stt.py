from __future__ import annotations

import asyncio
import importlib.util
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.speech.vad import AudioMetrics, SileroVAD, VADConfig, VADResult


logger = logging.getLogger("nyra.microphone")


class Transcription(BaseModel):
    text: str
    language: str
    language_probability: float
    duration_seconds: float
    transcription_seconds: float
    audio_metrics: AudioMetrics
    vad: VADResult


class STTProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def health(self) -> bool: ...

    @abstractmethod
    async def transcribe(self, path: Path) -> Transcription: ...


class StreamingSTTProvider(STTProvider):
    """Future partial-transcription contract; partials are never sent to the LLM automatically."""

    @abstractmethod
    async def stream_transcribe(self, source) -> AsyncIterator[str]: ...


class FasterWhisperSTT(STTProvider):
    def __init__(
        self,
        model_name: str = "tiny",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "pt",
        beam_size: int = 1,
        cpu_threads: int = 4,
        workers: int = 1,
        vad_config: VADConfig | None = None,
        gain: float = 1.0,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self.cpu_threads = cpu_threads
        self.workers = workers
        self.vad = SileroVAD(vad_config or VADConfig(), gain=gain)
        self._model: Any = None
        self._load_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "faster_whisper"

    async def health(self) -> bool:
        return importlib.util.find_spec("faster_whisper") is not None

    async def preload(self) -> None:
        await self._get_model()

    async def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = await asyncio.to_thread(
                    WhisperModel,
                    self.model_name,
                    device=self.device,
                    compute_type=self.compute_type,
                    cpu_threads=self.cpu_threads,
                    num_workers=self.workers,
                )
        return self._model

    async def transcribe(self, path: Path) -> Transcription:
        if not path.exists() or path.stat().st_size == 0:
            raise ValueError("Arquivo de áudio vazio")
        prepared = await asyncio.to_thread(self.vad.prepare, path)
        if prepared.vad.enabled and not prepared.vad.speech_detected:
            logger.info(
                "speech_rejected_by_vad",
                extra={"rms": prepared.metrics.rms, "peak": prepared.metrics.peak},
            )
            return Transcription(
                text="",
                language=self.language,
                language_probability=0.0,
                duration_seconds=prepared.metrics.duration_seconds,
                transcription_seconds=0.0,
                audio_metrics=prepared.metrics,
                vad=prepared.vad,
            )
        model = await self._get_model()
        started = time.perf_counter()

        def run() -> tuple[str, Any]:
            segments, info = model.transcribe(
                prepared.samples,
                language=self.language,
                vad_filter=self.vad.config.enabled,
                vad_parameters={
                    "threshold": self.vad.config.threshold,
                    "min_speech_duration_ms": self.vad.config.min_speech_ms,
                    "min_silence_duration_ms": self.vad.config.min_silence_ms,
                    "speech_pad_ms": self.vad.config.speech_pad_ms,
                },
                beam_size=self.beam_size,
                temperature=0.0,
                condition_on_previous_text=False,
            )
            return " ".join(segment.text.strip() for segment in segments).strip(), info

        text, info = await asyncio.to_thread(run)
        elapsed = time.perf_counter() - started
        logger.info(
            "speech_transcribed",
            extra={
                "duration_seconds": prepared.metrics.duration_seconds,
                "transcription_seconds": round(elapsed, 3),
                "rms": prepared.metrics.rms,
                "peak": prepared.metrics.peak,
                "clipping": prepared.metrics.clipping,
                "vad_speech_ms": prepared.vad.speech_duration_ms,
            },
        )
        return Transcription(
            text=text,
            language=getattr(info, "language", self.language),
            language_probability=float(getattr(info, "language_probability", 1.0)),
            duration_seconds=float(getattr(info, "duration", 0.0)),
            transcription_seconds=round(elapsed, 3),
            audio_metrics=prepared.metrics,
            vad=prepared.vad,
        )

    @property
    def loaded(self) -> bool:
        return self._model is not None
