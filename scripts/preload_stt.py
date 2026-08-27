"""Download exactly the configured faster-whisper model and validate local inference setup."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from faster_whisper import WhisperModel  # noqa: E402
from app.core.config import Settings  # noqa: E402

settings = Settings.from_sources()
print(f"Preloading STT model: {settings.stt_model} ({settings.stt_device}/{settings.stt_compute_type})")
WhisperModel(
    settings.stt_model,
    device=settings.stt_device,
    compute_type=settings.stt_compute_type,
)
print("STT model ready")
