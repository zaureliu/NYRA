"""Servidor de teste local para o VoiceProcessorBridge (prompt11 Parte AM §195).

Simula um processador de voz externo REAL na prática mais simples possível:

    python scripts/test_external_voice_processor.py [--port 8977] [--fail-after N]

* GET /health  → 200 com name/version/healthy/capabilities (negociação §125)
* POST /stt    → echo JSON (placeholder honesto: não é ASR real)
* POST /tts    → WAV mínimo válido (não é voz real)

Escuta APENAS em 127.0.0.1 (§123 — nunca LAN).  --fail-after faz /health
passar a responder 500 após N chamadas, para exercitar queda + fallback +
circuit breaker sem depender de hardware.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI  # noqa: E402
import uvicorn  # noqa: E402

app = FastAPI()
_state = {"health_calls": 0, "fail_after": 0}


def _minimal_wav() -> bytes:
    sample_rate = 16000
    samples = [int(12000 * ((i % 32) < 16) * 1 for i in range(sample_rate // 4))]
    data = b"".join(struct.pack("<h", s) for s in samples)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data


@app.get("/health")
async def health() -> dict:
    _state["health_calls"] += 1
    if _state["fail_after"] and _state["health_calls"] > _state["fail_after"]:
        from fastapi.responses import JSONResponse

        return JSONResponse({"healthy": False}, status_code=500)
    return {
        "name": "nyra-test-voice-processor",
        "version": "0.1.0",
        "healthy": True,
        "capabilities": {
            "stt": True,
            "tts": True,
            "vad": True,
            "aec": False,
            "ns": True,
            "streaming": False,
        },
    }


@app.post("/stt")
async def stt(payload: dict) -> dict:
    return {"text": "[echo stt]", "confidence": 0.0, "simulated": True}


@app.post("/tts")
async def tts() -> bytes:
    from fastapi.responses import Response

    return Response(content=_minimal_wav(), media_type="audio/wav")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8977)
    parser.add_argument("--fail-after", type=int, default=0,
                        help="/health responde 500 após N chamadas (teste de queda)")
    args = parser.parse_args()
    _state["fail_after"] = args.fail_after
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
