"""Isolated, allowlisted OmniVoice BR-PT voice-design worker.

This worker never accepts a reference recording. It creates a synthetic voice from
text attributes, so no third-party biometric identity enters the KAZUMI pipeline.
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

ALLOWED_MODEL = "edwixx/omnivoice-brpt-v15"
DEFAULT_INSTRUCTION = (
    "female, young adult, low pitch, portuguese accent"
)


def render(model_id: str, text: str, output: Path, seed: int, steps: int) -> dict:
    if model_id != ALLOWED_MODEL:
        raise ValueError("modelo OmniVoice fora da allowlist")
    if not 1 <= len(text) <= 1200:
        raise ValueError("texto deve ter entre 1 e 1200 caracteres")
    output = output.resolve()
    project_root = Path(__file__).resolve().parents[3]
    allowed_root = (project_root / "data").resolve()
    if allowed_root not in output.parents:
        raise ValueError("saída fora de data/")

    import numpy as np
    import soundfile as sf
    import torch
    from omnivoice import OmniVoice, OmniVoiceGenerationConfig

    random.seed(seed); np.random.seed(seed); torch.manual_seed(seed)
    load_started = time.perf_counter()
    model = OmniVoice.from_pretrained(model_id, device_map="cpu", dtype=torch.float32, load_asr=False)
    load_ms = round((time.perf_counter() - load_started) * 1000, 1)
    generation = OmniVoiceGenerationConfig(num_step=steps, guidance_scale=2.2)
    started = time.perf_counter()
    audio = model.generate(
        text=text, language="pt", instruct=DEFAULT_INSTRUCTION, speed=0.94,
        generation_config=generation,
    )[0]
    generation_ms = round((time.perf_counter() - started) * 1000, 1)
    output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output), np.asarray(audio, dtype=np.float32), model.sampling_rate, subtype="PCM_16")
    duration_ms = round(len(audio) / model.sampling_rate * 1000, 1)
    return {
        "model": model_id, "output": str(output), "seed": seed, "steps": steps,
        "sample_rate": model.sampling_rate, "load_ms": load_ms,
        "generation_ms": generation_ms, "total_ms": load_ms + generation_ms,
        "audio_duration_ms": duration_ms,
        "real_time_factor": round(generation_ms / max(duration_ms, 1), 3),
        "instruction": DEFAULT_INSTRUCTION,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-id", default=ALLOWED_MODEL)
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=3404)
    parser.add_argument("--steps", type=int, choices=(8, 16, 24, 32), default=24)
    args = parser.parse_args()
    print(json.dumps(render(args.model_id, args.text, args.output, args.seed, args.steps), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
