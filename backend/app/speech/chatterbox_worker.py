"""Isolated Chatterbox Multilingual worker.

It receives a trusted local JSON request path from ChatterboxTTSProvider. It never
accepts shell commands and never downloads or chooses a real person's voice.
"""
from __future__ import annotations

import json
import random
import sys
import contextlib
from pathlib import Path


def _load_model(device: str, model_id: str):
    import torch
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    if model_id != "ResembleAI/chatterbox":
        raise RuntimeError(f"checkpoint especializado requer loader dedicado: {model_id}")
    # chatterbox-tts 0.1.7 has the V3 checkpoint as its default and does not
    # accept the newer optional t3_model keyword.
    return ChatterboxMultilingualTTS.from_pretrained(device=device)


def probe(model_id: str = "ResembleAI/chatterbox") -> int:
    import torch
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS  # noqa: F401

    if model_id != "ResembleAI/chatterbox":
        print(json.dumps({"available": False, "model_id": model_id, "reason": "dedicated checkpoint requires loader/assets"}))
        return 5

    print(
        json.dumps(
            {
                "torch": torch.__version__,
                "cuda": torch.cuda.is_available(),
                "device": "cuda" if torch.cuda.is_available() else "cpu",
                "model_id": model_id,
            }
        )
    )
    return 0


def render(request: dict, model) -> int:
    import numpy as np
    import soundfile as sf
    import torch
    output = Path(request["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    seed = int(request.get("seed", 42))
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    reference = request.get("audio_prompt_path")
    chunks = [part.strip() for part in request["text"].split("\n\n") if part.strip()]
    audio: list[np.ndarray] = []
    for index, chunk in enumerate(chunks):
        kwargs = {
            "language_id": request.get("language_id", "pt"),
            "exaggeration": float(request.get("exaggeration", 0.5)),
            "temperature": float(request.get("temperature", 0.8)),
            "cfg_weight": float(request.get("cfg_weight", 0.45)),
        }
        if reference and Path(reference).is_file():
            kwargs["audio_prompt_path"] = reference
        wav = model.generate(chunk[:300], **kwargs)
        audio.append(wav.detach().cpu().numpy().reshape(-1).astype(np.float32))
        if index < len(chunks) - 1:
            pause_ms = int(request.get("paragraph_pause_ms", 420))
            audio.append(np.zeros(round(model.sr * pause_ms / 1000), dtype=np.float32))
    if not audio:
        return 2
    joined = np.concatenate(audio)
    peak = float(np.max(np.abs(joined)))
    if peak > 0:
        joined = np.clip(joined * min(1.1, 0.92 / peak), -0.96, 0.96)
    sf.write(str(output), joined, model.sr, subtype="PCM_16")
    return 0 if output.is_file() and output.stat().st_size > 100 else 3


def run(request_path: Path) -> int:
    import torch
    request = json.loads(request_path.read_text(encoding="utf-8"))
    device = request.get("device", "cpu")
    if device == "cuda" and not torch.cuda.is_available():
        device = "cpu"
    model = _load_model(device, request.get("model_id", "ResembleAI/chatterbox"))
    return render(request, model)


def server(model_id: str, device: str) -> int:
    import torch
    if device == "cuda" and not torch.cuda.is_available():
        device = "cpu"
    try:
        with contextlib.redirect_stdout(sys.stderr):
            model = _load_model(device, model_id)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}), flush=True)
        return 4
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                code = render(request, model)
            print(json.dumps({"ok": code == 0, "code": code}), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": type(exc).__name__}), flush=True)
    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--probe":
        return probe(sys.argv[sys.argv.index("--model-id") + 1] if "--model-id" in sys.argv else "ResembleAI/chatterbox")
    if len(sys.argv) >= 2 and sys.argv[1] == "--server":
        model_id = sys.argv[sys.argv.index("--model-id") + 1] if "--model-id" in sys.argv else "ResembleAI/chatterbox"
        return server(model_id, "cuda" if "--cuda" in sys.argv else "cpu")
    if len(sys.argv) != 2:
        return 2
    return run(Path(sys.argv[1]).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
