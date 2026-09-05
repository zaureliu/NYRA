"""Generate the fixed Voice 2.0 phrases through the running local API."""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

PHRASES = {
    "casual": "Oi... eu sou a Kazumi. Acho que finalmente encontrei uma voz que combina comigo.",
    "normal": "Eu estava olhando a rede. Por enquanto, tá tudo tranquilo.",
    "tecnica": "O Proxmox está online. Nenhuma máquina virtual apresentou falha.",
    "curiosa": "Hmm... apareceu um dispositivo diferente aqui. Quer que eu veja o que é?",
    "humor-seco": "DNS funcionando normalmente. Impressionante.",
    "alerta": "Espera... o servidor web acabou de parar de responder.",
}

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("kokoro", "chatterbox_multilingual_v3", "chatterbox_ptbr"), default="kokoro")
    parser.add_argument("--only", choices=tuple(PHRASES))
    args = parser.parse_args()
    output = Path(__file__).resolve().parents[1] / "data" / "voice-benchmarks" / args.provider
    output.mkdir(parents=True, exist_ok=True)
    selected = {args.only: PHRASES[args.only]} if args.only else PHRASES
    report = []
    for name, text in selected.items():
        payload = {
            "provider": args.provider, "voice": "pf_dora" if args.provider == "kokoro" else "default",
            "text": text, "state": "concerned" if name == "alerta" else "curious" if name == "curiosa" else "amused" if name == "humor-seco" else "neutral",
            "speaking_rate": .88,
            "temperature": .8, "exaggeration": .5, "cfg_weight": .45, "seed": 42,
            "sentence_pause_ms": 240, "paragraph_pause_ms": 460,
        }
        started = time.perf_counter()
        request = urllib.request.Request("http://127.0.0.1:8000/api/voice/synthesize", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=1000) as response:
            result = json.load(response)
        with urllib.request.urlopen("http://127.0.0.1:8000" + result["audio_url"], timeout=60) as response:
            audio = response.read()
        target = output / f"{name}.wav"; target.write_bytes(audio)
        report.append({"name": name, "seconds": round(time.perf_counter() - started, 3), "bytes": len(audio), "speech_text": result["speech_text"], "debug": result.get("debug")})
        print(f"{name}: {report[-1]['seconds']}s, {len(audio)} bytes")
    (output / "benchmark.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

if __name__ == "__main__":
    main()
