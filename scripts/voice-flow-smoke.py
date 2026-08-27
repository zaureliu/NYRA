from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import time

import httpx


async def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke local STT -> NYRA -> TTS sem expor a transcricao.")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    audio = args.audio.resolve()
    if not audio.is_file():
        raise SystemExit(f"audio ausente: {audio}")

    observed = {
        "speaking_guard_true": False,
        "lease_active": False,
        "microphone": False,
        "enabled": False,
        "modes": set(),
    }
    stop_polling = asyncio.Event()

    async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=5)) as client:
        async def poll_status() -> None:
            while not stop_polling.is_set():
                try:
                    status = (await client.get(f"{args.base_url}/api/listening/status")).json()
                    observed["speaking_guard_true"] |= bool(status.get("speaking_guard"))
                    observed["lease_active"] |= bool(status.get("lease_active"))
                    observed["microphone"] |= bool(status.get("microphone"))
                    observed["enabled"] |= bool(status.get("enabled"))
                    observed["modes"].add(str(status.get("mode")))
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(0.1)

        poll_task = asyncio.create_task(poll_status())
        stt_started = time.perf_counter()
        with audio.open("rb") as source:
            response = await client.post(
                f"{args.base_url}/api/speech/transcribe",
                files={"audio": (audio.name, source, "audio/wav")},
            )
        response.raise_for_status()
        transcription = response.json()
        stt_ms = round((time.perf_counter() - stt_started) * 1000, 1)
        text = str(transcription.get("text") or "").strip()
        if not text:
            stop_polling.set()
            await poll_task
            print(json.dumps({"ok": False, "stage": "stt", "stt_ms": stt_ms, "text_characters": 0}))
            return 2

        chat_started = time.perf_counter()
        response = await client.post(
            f"{args.base_url}/api/chat",
            json={"message": text, "synthesize": True},
        )
        response.raise_for_status()
        result = response.json()
        chat_ms = round((time.perf_counter() - chat_started) * 1000, 1)

        # The packaged dashboard/Desktop Presence receives the audio event and
        # toggles the backend playback guard. Give real playback time to start.
        deadline = time.perf_counter() + 20
        while time.perf_counter() < deadline and not observed["speaking_guard_true"]:
            await asyncio.sleep(0.1)
        if observed["speaking_guard_true"]:
            while time.perf_counter() < deadline:
                status = (await client.get(f"{args.base_url}/api/listening/status")).json()
                if not status.get("speaking_guard"):
                    break
                await asyncio.sleep(0.1)

        stop_polling.set()
        await poll_task

    timing = result.get("timing") or {}
    output = {
        "ok": True,
        "stt_provider": transcription.get("provider"),
        "stt_language": transcription.get("language"),
        "stt_ms": stt_ms,
        "text_characters": len(text),
        "request_id": result.get("response_id"),
        "ollama_first_token_ms": timing.get("ollama_first_token_ms"),
        "ollama_total_ms": timing.get("ollama_total_ms"),
        "tts_first_audio_ms": timing.get("tts_first_audio_ms"),
        "request_total_ms": timing.get("request_total_ms"),
        "client_total_ms": chat_ms,
        "audio_chunks": len(result.get("audio_urls") or []),
        "hands_on_enabled": observed["enabled"],
        "lease_active": observed["lease_active"],
        "microphone": observed["microphone"],
        "modes": sorted(observed["modes"]),
        "speaking_guard_observed": observed["speaking_guard_true"],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
