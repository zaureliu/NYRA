"""Isolated SAPI worker. It accepts file paths only; no shell is involved."""
from __future__ import annotations

import sys
from pathlib import Path

import pyttsx3


def choose_voice(voices: list):
    scored = []
    for voice in voices:
        descriptor = " ".join(
            [str(getattr(voice, "name", "")), str(getattr(voice, "id", ""))]
            + [str(item) for item in getattr(voice, "languages", [])]
        ).casefold()
        score = 10 if any(token in descriptor for token in ("portugu", "pt-br", "brazil", "maria", "francisca")) else 0
        score += 4 if any(token in descriptor for token in ("female", "maria", "francisca", "helena")) else 0
        scored.append((score, voice))
    return max(scored, key=lambda item: item[0])[1] if scored else None


def main() -> int:
    engine = pyttsx3.init()
    voices = engine.getProperty("voices") or []
    if len(sys.argv) == 2 and sys.argv[1] == "--probe":
        engine.stop()
        return 0 if voices else 2
    if len(sys.argv) != 4:
        return 2
    input_path, output_path, rate = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
    voice = choose_voice(voices)
    if voice is not None:
        engine.setProperty("voice", voice.id)
    engine.setProperty("rate", int(185 * rate))
    engine.setProperty("volume", 0.95)
    engine.save_to_file(input_path.read_text(encoding="utf-8"), str(output_path))
    engine.runAndWait()
    engine.stop()
    return 0 if output_path.is_file() and output_path.stat().st_size > 100 else 3


if __name__ == "__main__":
    raise SystemExit(main())
