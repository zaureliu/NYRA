from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

import httpx


OLLAMA = "http://127.0.0.1:11434"
KAZUMI = "http://127.0.0.1:8000"
MODEL = "qwen3:8b"
QUESTIONS = [
    "Responda em uma frase: você está online?",
    "Em uma frase, o que é DNS?",
    "Diga apenas uma recomendação curta para reduzir latência de rede.",
    "Responda brevemente: para que serve um backup?",
    "Em uma frase, explique o que é um container Docker.",
]


async def direct(client: httpx.AsyncClient, question: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": question}],
        "stream": True,
        "think": False,
        "keep_alive": "30m",
        "options": {"num_ctx": 8192, "num_predict": 96, "temperature": 0.2},
    }
    started = time.perf_counter()
    first = None
    final = {}
    chunks = []
    async with client.stream("POST", f"{OLLAMA}/api/chat", json=payload) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line:
                continue
            data = json.loads(line)
            token = str(data.get("message", {}).get("content", ""))
            if token:
                first = first or time.perf_counter()
                chunks.append(token)
            if data.get("done"):
                final = data
                break
    ended = time.perf_counter()
    return {
        "first_token_ms": round(((first or ended) - started) * 1000, 1),
        "generation_ms": round((ended - (first or ended)) * 1000, 1),
        "total_ms": round((ended - started) * 1000, 1),
        "server_total_ms": round(float(final.get("total_duration") or 0) / 1e6, 1),
        "load_ms": round(float(final.get("load_duration") or 0) / 1e6, 1),
        "characters": len("".join(chunks)),
    }


async def through_kazumi(client: httpx.AsyncClient, question: str) -> dict:
    started = time.perf_counter()
    response = await client.post(f"{KAZUMI}/api/chat", json={"message": question, "synthesize": False})
    response.raise_for_status()
    ended = time.perf_counter()
    value = response.json()
    timing = value.get("timing", {})
    return {
        "request_id": value.get("response_id"),
        "first_token_ms": timing.get("ollama_first_token_ms"),
        "generation_ms": timing.get("ollama_generation_ms"),
        "ollama_total_ms": timing.get("ollama_total_ms"),
        "total_ms": round((ended - started) * 1000, 1),
        "memory_ms": timing.get("memory_lookup_ms"),
        "context_ms": timing.get("context_build_ms"),
        "tools_ms": timing.get("tools_ms"),
        "prompt_characters": timing.get("prompt_characters"),
    }


async def main() -> None:
    results = []
    timeout = httpx.Timeout(90, connect=5)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for index, question in enumerate(QUESTIONS, 1):
            direct_result = await direct(client, question)
            kazumi_result = await through_kazumi(client, question)
            results.append({"index": index, "question": question, "direct": direct_result, "kazumi": kazumi_result})
            print(json.dumps(results[-1], ensure_ascii=False))
    output = Path(__file__).resolve().parents[1] / "logs" / "ollama-kazumi-benchmark.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"model": MODEL, "runs": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"saved={output}")


if __name__ == "__main__":
    asyncio.run(main())
