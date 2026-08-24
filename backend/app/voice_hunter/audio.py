from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from .models import AudioAnalysis


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def analyze_audio(path: Path) -> AudioAnalysis:
    import soundfile as sf

    samples, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = samples.mean(axis=1)
    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(mono)))) if mono.size else 0.0
    rms_dbfs = 20 * np.log10(max(rms, 1e-9))
    clipping = float(np.mean(np.abs(mono) >= 0.999)) if mono.size else 0.0
    silence = float(np.mean(np.abs(mono) < 0.01)) if mono.size else 1.0
    active = np.abs(mono) >= 0.01
    noise_rms = float(np.sqrt(np.mean(np.square(mono[~active])))) if np.any(~active) else 1e-9
    signal_rms = float(np.sqrt(np.mean(np.square(mono[active])))) if np.any(active) else 0.0
    snr = 20 * np.log10(max(signal_rms, 1e-9) / max(noise_rms, 1e-9))
    reasons: list[str] = []
    duration = len(mono) / rate if rate else 0.0
    if duration < 1.0:
        reasons.append("menos de um segundo de áudio")
    if silence > 0.72:
        reasons.append("silêncio excessivo")
    if clipping > 0.005:
        reasons.append("clipping severo")
    if peak < 0.01:
        reasons.append("nível de áudio insuficiente")
    return AudioAnalysis(
        duration_s=round(duration, 3), sample_rate=int(rate), channels=int(samples.shape[1]),
        rms_dbfs=round(float(rms_dbfs), 2), peak=round(peak, 6),
        clipping_ratio=round(clipping, 7), silence_ratio=round(silence, 5),
        approximate_snr_db=round(float(snr), 2), acceptable=not reasons,
        rejection_reasons=reasons,
    )


def normalize_candidate_audio(source: Path, destination: Path, target_rate: int = 24000) -> AudioAnalysis:
    import soundfile as sf

    samples, rate = sf.read(str(source), dtype="float32", always_2d=True)
    data = samples.mean(axis=1).astype(np.float32)
    if not data.size or not np.isfinite(data).all():
        raise ValueError("áudio vazio ou inválido")
    data -= float(data.mean())
    threshold = max(0.0025, float(np.max(np.abs(data))) * 0.012)
    active = np.flatnonzero(np.abs(data) > threshold)
    if active.size:
        pad = min(round(rate * 0.15), len(data) // 10)
        data = data[max(0, int(active[0]) - pad):min(len(data), int(active[-1]) + pad + 1)]
    if rate != target_rate:
        # Dependency-free linear resampling is deliberately conservative. Voice Hunter
        # uses this for short audition/reference clips, not as a mastering stage.
        target_length = max(1, round(len(data) * target_rate / rate))
        source_axis = np.linspace(0.0, 1.0, len(data), endpoint=False)
        target_axis = np.linspace(0.0, 1.0, target_length, endpoint=False)
        data = np.interp(target_axis, source_axis, data).astype(np.float32)
        rate = target_rate
    peak = float(np.max(np.abs(data)))
    if peak > 0.89:
        data *= 0.89 / peak
    destination.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(destination), np.clip(data, -0.95, 0.95), rate, subtype="PCM_16")
    return analyze_audio(destination)


def duplicate_hashes(paths: list[Path]) -> dict[str, list[Path]]:
    grouped: dict[str, list[Path]] = {}
    for path in paths:
        if path.is_file():
            grouped.setdefault(sha256_file(path), []).append(path)
    return {digest: values for digest, values in grouped.items() if len(values) > 1}
