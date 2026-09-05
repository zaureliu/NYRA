from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, Field


class VADConfig(BaseModel):
    enabled: bool = True
    threshold: float = Field(0.5, ge=0, le=1)
    min_speech_ms: int = Field(250, ge=50, le=5000)
    min_silence_ms: int = Field(650, ge=100, le=5000)
    speech_pad_ms: int = Field(250, ge=0, le=1000)


class AudioMetrics(BaseModel):
    rms: float
    peak: float
    clipping: bool
    clipping_ratio: float
    duration_seconds: float
    sample_rate: int = 16000
    channels: int = 1
    applied_gain: float = 1.0


class VADResult(BaseModel):
    enabled: bool
    engine: str
    speech_detected: bool
    speech_duration_ms: int
    segments: list[dict[str, int]]


@dataclass(frozen=True)
class PreparedAudio:
    samples: np.ndarray
    metrics: AudioMetrics
    vad: VADResult


class SileroVAD:
    """Uses faster-whisper's bundled Silero ONNX model; no cloud or PyTorch."""

    def __init__(self, config: VADConfig, gain: float = 1.0, sample_rate: int = 16000) -> None:
        self.config = config
        self.gain = gain
        self.sample_rate = sample_rate

    def prepare(self, path: Path) -> PreparedAudio:
        from faster_whisper.audio import decode_audio

        raw = decode_audio(str(path) if isinstance(path, Path) else path, sampling_rate=self.sample_rate).astype(np.float32)
        if raw.size == 0:
            raise ValueError("Arquivo de áudio sem amostras")
        raw_rms = float(np.sqrt(np.mean(np.square(raw))))
        peak = float(np.max(np.abs(raw)))
        clipping_ratio = float(np.mean(np.abs(raw) >= 0.99))
        automatic = 1.0
        if 0.001 < raw_rms < 0.045:
            automatic = min(2.5, 0.07 / raw_rms)
        applied_gain = min(4.0, self.gain * automatic)
        samples = np.clip(raw * applied_gain, -1.0, 1.0).astype(np.float32)

        segments: list[dict[str, int]] = []
        if self.config.enabled:
            from faster_whisper.vad import VadOptions, get_speech_timestamps

            options = VadOptions(
                threshold=self.config.threshold,
                min_speech_duration_ms=self.config.min_speech_ms,
                min_silence_duration_ms=self.config.min_silence_ms,
                speech_pad_ms=self.config.speech_pad_ms,
            )
            segments = get_speech_timestamps(samples, options, sampling_rate=self.sample_rate)
        else:
            segments = [{"start": 0, "end": int(samples.size)}]
        speech_samples = sum(max(0, item["end"] - item["start"]) for item in segments)
        return PreparedAudio(
            samples=samples,
            metrics=AudioMetrics(
                rms=round(raw_rms, 6),
                peak=round(peak, 6),
                clipping=clipping_ratio > 0.001,
                clipping_ratio=round(clipping_ratio, 6),
                duration_seconds=round(raw.size / self.sample_rate, 3),
                sample_rate=self.sample_rate,
                applied_gain=round(applied_gain, 3),
            ),
            vad=VADResult(
                enabled=self.config.enabled,
                engine="silero_v6_onnx" if self.config.enabled else "disabled",
                speech_detected=bool(segments),
                speech_duration_ms=round(speech_samples / self.sample_rate * 1000),
                segments=segments,
            ),
        )
