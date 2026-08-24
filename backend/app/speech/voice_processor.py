from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

import numpy as np
import soundfile as sf
from pydantic import BaseModel, Field

from app.core.paths import DATA_ROOT


class VoiceProcessorConfig(BaseModel):
    enabled: bool = True
    preset: Literal["natural", "focused", "concerned", "amused", "alert"] = "natural"
    high_pass_hz: float = Field(55, ge=0, le=180)
    eq_db: float = Field(0, ge=-6, le=6)
    compression: float = Field(0.18, ge=0, le=1)
    presence: float = Field(0.12, ge=0, le=1)
    output_gain_db: float = Field(-.5, ge=-12, le=6)
    signature_effect: float = Field(0.03, ge=0, le=.2)


PRESETS: dict[str, dict[str, float]] = {
    "natural": {},
    "focused": {"presence": .16, "compression": .22, "output_gain_db": -.7},
    "concerned": {"presence": .1, "compression": .24, "output_gain_db": -1.0},
    "amused": {"presence": .14, "compression": .16, "output_gain_db": -.6},
    "alert": {"presence": .2, "compression": .3, "output_gain_db": -1.2},
}


class VoiceProcessor:
    """Conservative offline DSP that preserves duration and fundamental pitch."""

    def __init__(self, config: VoiceProcessorConfig | None = None) -> None:
        self.config = config or VoiceProcessorConfig()
        self.output_dir = DATA_ROOT / "audio"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def update(self, config: VoiceProcessorConfig) -> VoiceProcessorConfig:
        self.config = config
        return self.config

    async def process(self, source: Path, state: str = "neutral") -> Path:
        if not self.config.enabled:
            return source
        output = self.output_dir / f"nyra-processed-{uuid4().hex}.wav"
        await asyncio.to_thread(self._process_sync, source, output, state)
        return output

    def _process_sync(self, source: Path, destination: Path, state: str) -> None:
        audio, sample_rate = sf.read(str(source), dtype="float32", always_2d=True)
        if not len(audio):
            raise ValueError("Áudio vazio")
        config = self._effective(state)
        processed = np.empty_like(audio)
        for channel in range(audio.shape[1]):
            value = audio[:, channel].astype(np.float64)
            value -= float(np.mean(value))
            spectrum = np.fft.rfft(value)
            frequencies = np.fft.rfftfreq(len(value), 1 / sample_rate)
            if config.high_pass_hz > 0:
                spectrum *= frequencies / np.sqrt(frequencies * frequencies + config.high_pass_hz**2)
            if config.eq_db:
                low_shape = np.exp(-.5 * ((frequencies - 240) / 260) ** 2)
                spectrum *= 10 ** ((config.eq_db * low_shape) / 20)
            if config.presence:
                presence_db = 1.8 * config.presence
                shape = np.exp(-.5 * ((frequencies - 3200) / 1700) ** 2)
                spectrum *= 10 ** ((presence_db * shape) / 20)
            value = np.fft.irfft(spectrum, n=len(value))
            value = self._compress(value, sample_rate, config.compression)
            if config.signature_effect:
                high = value - np.concatenate(([value[0]], value[:-1]))
                value = value + high * (.018 * config.signature_effect)
            value *= 10 ** (config.output_gain_db / 20)
            peak = float(np.max(np.abs(value)))
            if peak > .985:
                value *= .985 / peak
            processed[:, channel] = value.astype(np.float32)
        sf.write(str(destination), processed, sample_rate, subtype="PCM_16")

    @staticmethod
    def _compress(value: np.ndarray, sample_rate: int, amount: float) -> np.ndarray:
        if amount <= 0:
            return value
        frame = max(64, int(sample_rate * .012))
        count = math.ceil(len(value) / frame)
        gain = np.ones(count)
        threshold = 10 ** ((-14 - amount * 4) / 20)
        ratio = 1 + amount * 2.2
        for index in range(count):
            block = value[index * frame:(index + 1) * frame]
            rms = float(np.sqrt(np.mean(block * block) + 1e-12))
            if rms > threshold:
                target = threshold * (rms / threshold) ** (1 / ratio)
                gain[index] = target / rms
        points = np.minimum(np.arange(len(value)) // frame, count - 1)
        smooth = np.convolve(gain, np.ones(3) / 3, mode="same")
        return value * smooth[points]

    def _effective(self, state: str) -> VoiceProcessorConfig:
        preset = state if state in PRESETS and state != "natural" else self.config.preset
        values = {**self.config.model_dump(), **PRESETS.get(preset, {})}
        return VoiceProcessorConfig.model_validate(values)

    @staticmethod
    def analyze(path: Path) -> dict:
        audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
        mono = np.mean(audio, axis=1)
        peak = float(np.max(np.abs(mono))) if len(mono) else 0
        rms = float(np.sqrt(np.mean(mono * mono))) if len(mono) else 0
        return {
            "duration_ms": round(len(mono) / rate * 1000, 1), "sample_rate": rate,
            "peak": round(peak, 6), "rms": round(rms, 6), "clipping": peak >= .999,
            "channels": audio.shape[1],
        }
