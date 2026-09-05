from __future__ import annotations

from pathlib import Path

import numpy as np

from app.core.paths import DATA_ROOT

REFERENCE_PATH = DATA_ROOT / "voices" / "kazumi_reference.wav"
RECOMMENDATIONS = [
    "português brasileiro, uma única pessoa, ambiente silencioso",
    "aproximadamente 10–20 segundos (sem exigir duração exata)",
    "sem música, reverberação forte ou efeitos; volume consistente",
    "fala natural em tom neutro/calmo, sem pitch/formant artificial",
]


def inspect_reference(path: Path = REFERENCE_PATH) -> dict:
    result = {"present": path.is_file(), "valid": False, "path": "data/voices/kazumi_reference.wav", "recommendations": RECOMMENDATIONS}
    if not path.is_file():
        return result
    try:
        import soundfile as sf
        samples, rate = sf.read(str(path), dtype="float32", always_2d=True)
        data = samples.mean(axis=1)
        duration = len(data) / rate if rate else 0
        result.update({"sample_rate": rate, "channels": samples.shape[1], "duration_s": round(duration, 2), "peak": round(float(np.max(np.abs(data))), 4), "clipping": bool(np.max(np.abs(data)) >= 0.999), "valid": bool(rate > 0 and len(data) > 1000 and np.isfinite(data).all())})
    except Exception as exc:
        result["error"] = type(exc).__name__
    return result


def normalize_reference(source: Path, destination: Path = REFERENCE_PATH) -> dict:
    import soundfile as sf
    samples, rate = sf.read(str(source), dtype="float32", always_2d=True)
    data = samples.mean(axis=1).astype(np.float32)
    data -= float(data.mean())
    threshold = max(0.003, float(np.max(np.abs(data))) * 0.015)
    active = np.flatnonzero(np.abs(data) > threshold)
    if active.size:
        pad = min(rate // 5, len(data) // 10)
        data = data[max(0, int(active[0]) - pad):min(len(data), int(active[-1]) + pad + 1)]
    if rate != 24000:
        from scipy.signal import resample_poly
        data = resample_poly(data, 24000, rate).astype(np.float32)
        rate = 24000
    peak = float(np.max(np.abs(data))) if data.size else 0
    if peak > 0.89:
        data *= 0.89 / peak
    destination.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(destination), np.clip(data, -0.95, 0.95), rate, subtype="PCM_16")
    return inspect_reference(destination)
