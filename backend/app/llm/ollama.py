from __future__ import annotations

import logging
import json
import time
from collections.abc import AsyncIterator

import httpx

from app.llm.base import LLMMessage, LLMProvider, LLMResponse


logger = logging.getLogger("nyra")


def ollama_keep_alive_payload(value: str | int) -> str | int:
    """Ollama 0.32 accepts duration strings, but sentinel values are integers."""
    cleaned = str(value).strip()
    return int(cleaned) if cleaned in {"-1", "0"} else cleaned


class OllamaProvider(LLMProvider):
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 180,
        context_size: int = 8192,
        keep_alive: str = "-1",
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.context_size = context_size
        self.keep_alive = keep_alive
        self.last_runtime_metrics: dict[str, float] = {}

    @property
    def name(self) -> str:
        return "ollama"

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                models = response.json().get("models", [])
                return any(model.get("name") == self.model for model in models)
        except (httpx.HTTPError, ValueError):
            return False

    async def ready(self) -> bool:
        """Return quickly instead of queueing a chat behind Ollama's cold load."""
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(f"{self.base_url}/api/ps")
                response.raise_for_status()
                models = response.json().get("models", [])
                return self._is_resident(models, self.model)
        except (httpx.HTTPError, ValueError):
            return False

    @staticmethod
    def _is_resident(models: list[dict], model: str) -> bool:
        return any(item.get("name") == model or item.get("model") == model for item in models)

    async def chat(self, messages: list[LLMMessage]) -> str:
        response = await self.complete(messages)
        content = response.content.strip()
        if not content:
            raise RuntimeError("Ollama returned an empty response")
        return content

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        payload = {
            "model": self.model,
            "messages": [message.model_dump(exclude_none=True, exclude_defaults=True) for message in messages],
            "stream": False,
            "think": False,
            "keep_alive": ollama_keep_alive_payload(self.keep_alive),
            "options": {"temperature": 0.65, "top_p": 0.9, "num_ctx": self.context_size},
        }
        if tools:
            payload["tools"] = tools
        started = time.perf_counter()
        trace_marks: dict[str, float] = {}
        async def trace(name: str, _info: dict) -> None:
            if name == "connection.connect_tcp.started": trace_marks["started"] = time.perf_counter()
            if name == "connection.connect_tcp.complete": trace_marks["complete"] = time.perf_counter()
        data: dict = {}
        # Small models occasionally emit a single empty message mid-tool-loop;
        # one immediate retry keeps the turn alive without masking real errors.
        for attempt in range(2):
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload, extensions={"trace": trace})
                response.raise_for_status()
                data = response.json()
            self.last_runtime_metrics = self._runtime_metrics(data, started)
            message_candidate = LLMResponse.model_validate(data.get("message", {}))
            if message_candidate.content.strip() or message_candidate.tool_calls:
                break
            if attempt == 1:
                logger.warning("ollama_empty_response", extra={"model": self.model})
                raise RuntimeError("Ollama returned neither content nor tool calls")
            self.last_runtime_metrics["ollama_empty_retry"] = True
        self.last_runtime_metrics["ollama_connect_ms"] = self._connect_ms(trace_marks)
        message = LLMResponse.model_validate(data.get("message", {}))
        logger.info(
            "llm_response",
            extra={"provider": self.name, "model": self.model, "tool_calls": len(message.tool_calls)},
        )
        return message

    async def stream(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": [message.model_dump(exclude_none=True, exclude_defaults=True) for message in messages],
            "stream": True,
            "think": False,
            "keep_alive": ollama_keep_alive_payload(self.keep_alive),
            "options": {"temperature": 0.65, "top_p": 0.9, "num_ctx": self.context_size},
        }
        received = False
        final: dict = {}
        started = time.perf_counter()
        trace_marks: dict[str, float] = {}
        async def trace(name: str, _info: dict) -> None:
            if name == "connection.connect_tcp.started": trace_marks["started"] = time.perf_counter()
            if name == "connection.connect_tcp.complete": trace_marks["complete"] = time.perf_counter()
        timeout = httpx.Timeout(self.timeout, connect=5)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{self.base_url}/api/chat", json=payload, extensions={"trace": trace}) as response:
                response.raise_for_status()
                headers_received = time.perf_counter()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    content = str(data.get("message", {}).get("content", ""))
                    if content:
                        received = True
                        yield content
                    if data.get("done"):
                        final = data
                        break
        self.last_runtime_metrics = self._runtime_metrics(final, started)
        self.last_runtime_metrics["ollama_connect_ms"] = self._connect_ms(trace_marks)
        self.last_runtime_metrics["ollama_headers_ms"] = round((headers_received - started) * 1000, 1)
        if not received:
            raise RuntimeError("Ollama returned an empty stream")
        logger.info("llm_stream_finished", extra={"provider": self.name, "model": self.model})

    @staticmethod
    def _runtime_metrics(data: dict, started: float) -> dict[str, float]:
        milliseconds = lambda key: round(float(data.get(key) or 0) / 1_000_000, 1)
        return {
            "ollama_http_total_ms": round((time.perf_counter() - started) * 1000, 1),
            "ollama_server_total_ms": milliseconds("total_duration"),
            "ollama_load_ms": milliseconds("load_duration"),
            "ollama_prompt_eval_ms": milliseconds("prompt_eval_duration"),
            "ollama_eval_ms": milliseconds("eval_duration"),
        }

    @staticmethod
    def _connect_ms(marks: dict[str, float]) -> float:
        if "started" not in marks or "complete" not in marks:
            return 0.0
        return round((marks["complete"] - marks["started"]) * 1000, 1)
