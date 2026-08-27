from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.core.config import get_settings
from app.speech.stt import FasterWhisperSTT
from app.voice_hunter.audio import analyze_audio, sha256_file
from app.voice_hunter.models import STTValidation
from app.voice_hunter.service import VoiceHunterService


async def run(candidate_id: str) -> int:
    settings = get_settings()
    stt = FasterWhisperSTT(settings.stt_model, settings.stt_device, settings.stt_compute_type, settings.stt_language)
    hunter = VoiceHunterService(stt=stt)
    candidate = hunter.get_candidate(candidate_id)
    sample = hunter.sample_path(candidate_id)
    candidate.analysis = await asyncio.to_thread(analyze_audio, sample)
    transcript = await stt.transcribe(sample)
    candidate.stt = STTValidation(
        language=transcript.language, confidence=transcript.language_probability,
        transcription=transcript.text, duration_s=transcript.duration_seconds,
    )
    candidate.provenance["sha256"] = await asyncio.to_thread(sha256_file, sample)
    await hunter._persist()
    print(candidate.model_dump_json(indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate_id")
    return asyncio.run(run(parser.parse_args().candidate_id))


if __name__ == "__main__":
    raise SystemExit(main())
