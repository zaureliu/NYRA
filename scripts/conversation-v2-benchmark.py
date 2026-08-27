"""Reproducible NYRA cold/warm and perceived-feedback benchmark.

No prompt/response/audio content is stored.  `--unload-first` intentionally
removes only the selected Ollama model from residency before the measurement.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import time

import httpx
import websockets


def ns_ms(value) -> float:
    return round(float(value or 0) / 1_000_000, 1)


async def ollama_request(base_url: str, model: str, prompt: str, keep_alive: str) -> dict:
    started = time.perf_counter()
    first_chunk = None
    final = {}
    characters = 0
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "think": False,
        "keep_alive": keep_alive,
        "options": {"num_ctx": 8192, "num_predict": 48, "temperature": 0},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(360, connect=5)) as client:
        async with client.stream("POST", base_url.rstrip("/") + "/api/generate", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                item = json.loads(line)
                content = str(item.get("response") or "")
                if content and first_chunk is None:
                    first_chunk = (time.perf_counter() - started) * 1000
                characters += len(content)
                if item.get("done"):
                    final = item
    return {
        "ttft_ms": round(first_chunk or 0, 1),
        "complete_ms": round((time.perf_counter() - started) * 1000, 1),
        "load_duration_ms": ns_ms(final.get("load_duration")),
        "prompt_eval_duration_ms": ns_ms(final.get("prompt_eval_duration")),
        "eval_duration_ms": ns_ms(final.get("eval_duration")),
        "total_duration_ms": ns_ms(final.get("total_duration")),
        "response_characters": characters,
    }


async def nyra_turn(base_url: str, prompt: str) -> dict:
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/ws"
    request_sent = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    marks = {}
    response_id = None
    first_audio_url = None
    async with websockets.connect(ws_url, max_size=2**20) as websocket:
        await websocket.send("conversation-v2-benchmark")
        await websocket.recv()
        async with httpx.AsyncClient(timeout=360) as client:
            request = asyncio.create_task(client.post(
                base_url + "/api/chat", json={"message": prompt, "synthesize": True}
            ))
            while True:
                event = json.loads(await asyncio.wait_for(websocket.recv(), 360))
                kind, payload = event.get("type"), event.get("payload", {})
                candidate = payload.get("response_id")
                if response_id is None and kind == "USER_SPEECH_FINAL":
                    response_id = candidate
                if response_id and candidate not in {None, response_id}:
                    continue
                elapsed = round((time.perf_counter() - started) * 1000, 1)
                if kind == "LLM_TOKEN_RECEIVED":
                    marks.setdefault("first_visible_token_ms", elapsed)
                elif kind == "SENTENCE_READY":
                    marks.setdefault("first_sentence_ms", elapsed)
                elif kind == "TTS_CHUNK_STARTED":
                    marks.setdefault("tts_request_ms", elapsed)
                elif kind == "TTS_CHUNK_FINISHED":
                    marks.setdefault("first_tts_audio_ms", elapsed)
                    first_audio_url = first_audio_url or payload.get("audio_url")
                elif kind == "NYRA_RESPONSE":
                    marks.setdefault("response_event_ms", elapsed)
                if request.done() and "response_event_ms" in marks:
                    break
            response = await request
            response.raise_for_status()
            result = response.json()
            if first_audio_url:
                audio_started = time.perf_counter()
                audio = await client.get(base_url + str(first_audio_url))
                audio.raise_for_status()
                marks["first_playable_audio_ms"] = round((time.perf_counter() - started) * 1000, 1)
                marks["audio_download_ms"] = round((time.perf_counter() - audio_started) * 1000, 1)
                await client.post(base_url + "/api/listening/playback", json={
                    "playing": True, "response_id": response_id,
                })
                await client.post(base_url + "/api/listening/playback", json={"playing": False})
    return {
        "request_sent": request_sent,
        "response_id": response_id,
        **marks,
        "request_complete_ms": round((time.perf_counter() - started) * 1000, 1),
        "response_characters": len(result.get("response", "")),
        "audio_chunks": len(result.get("audio_urls", [])),
        "server_timing": result.get("timing", {}),
    }


async def main(args) -> dict:
    ollama = args.ollama_url.rstrip("/")
    nyra = args.nyra_url.rstrip("/")
    async with httpx.AsyncClient(timeout=360) as client:
        if args.unload_first:
            await client.post(ollama + "/api/generate", json={
                "model": args.model, "stream": False, "keep_alive": 0,
            })
        preload = None
        if args.preload:
            started = time.perf_counter()
            response = await client.post(nyra + "/api/ollama/preload")
            response.raise_for_status()
            preload = {**response.json(), "client_ms": round((time.perf_counter() - started) * 1000, 1)}
    direct = await ollama_request(ollama, args.model, args.text, args.keep_alive)
    turn = await nyra_turn(nyra, args.text)
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "unloaded_first": args.unload_first,
        "preload": preload,
        "direct_ollama": direct,
        "nyra_turn": turn,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--nyra-url", default="http://127.0.0.1:8000")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--keep-alive", default="1h")
    parser.add_argument("--text", default="Nyra, bom dia. Responda em uma frase curta.")
    parser.add_argument("--unload-first", action="store_true")
    parser.add_argument("--preload", action="store_true")
    print(json.dumps(asyncio.run(main(parser.parse_args())), ensure_ascii=False, indent=2))
