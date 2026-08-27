"""Real local V4 benchmark. Stores no prompt or response content."""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx
import websockets


async def benchmark(text: str, base_url: str) -> dict:
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/ws"
    async with websockets.connect(ws_url, max_size=2**20) as websocket:
        await websocket.send("benchmark-ready")
        connected = json.loads(await websocket.recv())
        started = time.perf_counter()
        marks: dict[str, float] = {}
        response_id = None
        async with httpx.AsyncClient(timeout=180) as client:
            request = asyncio.create_task(client.post(base_url + "/api/chat", json={"message": text, "synthesize": True}))
            while True:
                event = json.loads(await asyncio.wait_for(websocket.recv(), 180))
                event_type, payload = event.get("type"), event.get("payload", {})
                candidate = payload.get("response_id")
                if response_id is None and event_type == "USER_SPEECH_FINAL":
                    response_id = candidate
                if response_id and candidate not in {None, response_id}:
                    continue
                elapsed = (time.perf_counter() - started) * 1000
                if event_type == "LLM_TOKEN_RECEIVED": marks.setdefault("llm_first_token_ms", elapsed)
                if event_type == "SENTENCE_READY": marks.setdefault("first_sentence_ms", elapsed)
                if event_type == "TTS_CHUNK_STARTED": marks.setdefault("tts_start_ms", elapsed)
                if event_type == "TTS_CHUNK_FINISHED": marks.setdefault("first_audio_ms", elapsed)
                if event_type == "NYRA_RESPONSE":
                    marks.setdefault("response_event_ms", elapsed)
                if request.done() and "response_event_ms" in marks:
                    break
            response = await request
            response.raise_for_status()
            data = response.json()
        marks["request_complete_ms"] = round((time.perf_counter() - started) * 1000, 1)
        marks["response_id"] = response_id
        marks["response_characters"] = len(data.get("response", ""))
        marks["audio_chunks"] = len(data.get("audio_urls", []))
        marks["server_timing"] = data.get("timing", {})
        marks["websocket_connected"] = connected.get("type") == "CONNECTED"
        return {key: round(value, 1) if isinstance(value, float) else value for key, value in marks.items()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default="Nyra, você está online? Responda em uma frase curta.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(benchmark(args.text, args.base_url)), ensure_ascii=False, indent=2))
