from __future__ import annotations

import io
import os
import wave
from pathlib import Path

import numpy as np


MAX_PROVIDER_AUDIO_BYTES = 50 * 1024 * 1024


def write_wav_response(data: bytes, destination: Path) -> Path:
    """Validate and atomically persist a provider WAV response."""
    if not data or len(data) > MAX_PROVIDER_AUDIO_BYTES:
        raise ValueError("provider audio response is empty or too large")
    temporary = destination.with_suffix(".tmp.wav")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_bytes(data)
    try:
        with wave.open(str(temporary), "rb") as source:
            if source.getnchannels() < 1 or source.getframerate() < 8000 or source.getnframes() < 1:
                raise ValueError("provider returned an invalid WAV stream")
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination.resolve()

def decode_audio_response_to_wav(data: bytes, destination: Path) -> Path:
    """Decode one compressed provider response into the pipeline WAV format."""
    if not data or len(data) > MAX_PROVIDER_AUDIO_BYTES:
        raise ValueError("provider audio response is empty or too large")
    import av
    import soundfile as sf

    container = av.open(io.BytesIO(data))
    try:
        frames = [frame.to_ndarray() for frame in container.decode(audio=0)]
        if not frames:
            raise ValueError("provider returned empty audio")
        samples = np.concatenate(frames, axis=1)
        if samples.ndim > 1:
            samples = samples.mean(axis=0)
        samples = samples.astype(np.float32)
        rate = int(container.streams.audio[0].rate or 24000)
    finally:
        container.close()

    temporary = destination.with_suffix(".tmp.wav")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        sf.write(str(temporary), np.clip(samples, -1, 1), rate, subtype="PCM_16")
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination.resolve()
