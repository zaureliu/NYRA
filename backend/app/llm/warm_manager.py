from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import StrEnum
import logging
import time
from typing import Any

import httpx

from app.core.paths import IDENTITY_ROOT
from app.events import EventBus, EventType
from app.llm.ollama import ollama_keep_alive_payload


logger = logging.getLogger("kazumi.ollama_warm")


class OllamaReadiness(StrEnum):
    OLLAMA_OFFLINE = "OLLAMA_OFFLINE"
    OLLAMA_LOADING = "OLLAMA_LOADING"
    OLLAMA_READY = "OLLAMA_READY"
    OLLAMA_ERROR = "OLLAMA_ERROR"


class OllamaWarmManager:
    """Owns Ollama preload, keep-alive, recovery and model-change readiness."""

    def __init__(self, settings, brain, event_bus: EventBus) -> None:
        self.settings = settings
        self.brain = brain
        self.event_bus = event_bus
        self.state = OllamaReadiness.OLLAMA_OFFLINE
        self.model: str | None = None
        self.last_error: str | None = None
        self.metrics: dict[str, Any] = {}
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._preload_lock = asyncio.Lock()
        self._stopping = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._monitor(), name="kazumi-ollama-warm-manager")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def request_rewarm(self) -> None:
        self._wake.set()

    async def preload(self, *, force: bool = False) -> dict[str, Any]:
        async with self._preload_lock:
            return await self._preload(force=force)

    async def _preload(self, *, force: bool = False) -> dict[str, Any]:
        target = str(getattr(self.brain, "model", self.settings.llm_model))
        if not force and self.state == OllamaReadiness.OLLAMA_READY and self.model == target and await self.brain.ready():
            return self.status()
        previous = self.model
        await self._set_state(OllamaReadiness.OLLAMA_LOADING, target)
        started = time.perf_counter()
        preload_data: dict[str, Any] = {}
        try:
            if previous and previous != target and self.settings.ollama_unload_previous_model:
                await self._unload(previous)
            payload = {
                "model": target,
                "stream": False,
                "keep_alive": ollama_keep_alive_payload(self.settings.ollama_keep_alive),
            }
            if not await self.brain.ready():
                preload_data = await self._load_weights(payload)
            warmup_metrics = await self._warmup(target) if self.settings.ollama_warmup else {}
            if not await self.brain.ready():
                raise RuntimeError("Ollama completed preload but the model is not resident")
            self.model = target
            self.last_error = None
            self.metrics = {
                "preload_total_ms": round((time.perf_counter() - started) * 1000, 1),
                "load_duration_ms": self._ns_ms(preload_data.get("load_duration")),
                "total_duration_ms": self._ns_ms(preload_data.get("total_duration")),
                "resident": True,
                **warmup_metrics,
            }
            await self._set_state(OllamaReadiness.OLLAMA_READY, target)
            logger.info("ollama_model_ready", extra={"model": target, "keep_alive": self.settings.ollama_keep_alive, **self.metrics})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = type(exc).__name__
            self.metrics = {"preload_total_ms": round((time.perf_counter() - started) * 1000, 1)}
            offline = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
            await self._set_state(OllamaReadiness.OLLAMA_OFFLINE if offline else OllamaReadiness.OLLAMA_ERROR, target)
            logger.warning("ollama_preload_failed", extra={"model": target, "error_type": self.last_error})
        return self.status()

    async def _load_weights(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Use Ollama's empty generate preload without trusting a stuck response.

        Ollama 0.32.15 can expose the model in `/api/ps` before completing the
        empty-request response.  Residency is the useful readiness signal, so
        observe it while the official preload request is in flight.
        """
        timeout = self.settings.ollama_preload_timeout_seconds
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5)) as client:
            request = asyncio.create_task(
                client.post(f"{self.settings.ollama_url}/api/generate", json=payload),
                name="kazumi-ollama-empty-preload",
            )
            deadline = time.monotonic() + timeout
            try:
                while time.monotonic() < deadline:
                    done, _ = await asyncio.wait({request}, timeout=.5)
                    if done:
                        response = request.result()
                        response.raise_for_status()
                        return response.json()
                    if await self.brain.ready():
                        request.cancel()
                        try:
                            await request
                        except asyncio.CancelledError:
                            pass
                        return {"resident_observed_before_response": True}
                request.cancel()
                try:
                    await request
                except asyncio.CancelledError:
                    pass
                raise TimeoutError("Ollama preload exceeded configured timeout")
            except BaseException:
                if not request.done():
                    request.cancel()
                raise

    async def _warmup(self, model: str) -> dict[str, Any]:
        # Prime the same chat/system-prefix path used by real turns. This request
        # remains isolated: no conversation history, memory, tools, events or TTS.
        system_prompt = (IDENTITY_ROOT / "system_prompt.md").read_text(encoding="utf-8")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Responda apenas OK."},
            ],
            "stream": False,
            "think": False,
            "keep_alive": ollama_keep_alive_payload(self.settings.ollama_keep_alive),
            "options": {"num_ctx": self.settings.ollama_context_size, "num_predict": 1, "temperature": 0},
        }
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.settings.ollama_preload_timeout_seconds, connect=5)) as client:
            response = await client.post(f"{self.settings.ollama_url}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
        return {
            "warmup_total_ms": round((time.perf_counter() - started) * 1000, 1),
            "warmup_load_ms": self._ns_ms(data.get("load_duration")),
            "warmup_prompt_eval_ms": self._ns_ms(data.get("prompt_eval_duration")),
            "warmup_eval_ms": self._ns_ms(data.get("eval_duration")),
        }

    async def _unload(self, model: str) -> None:
        payload = {"model": model, "stream": False, "keep_alive": 0}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(f"{self.settings.ollama_url}/api/generate", json=payload)
        except httpx.HTTPError:
            logger.warning("ollama_previous_model_unload_failed", extra={"model": model})

    async def _monitor(self) -> None:
        if self.settings.ollama_preload:
            await self.preload()
        while not self._stopping:
            try:
                self._wake.clear()
                try:
                    await asyncio.wait_for(
                        self._wake.wait(),
                        timeout=self.settings.ollama_recovery_interval_seconds,
                    )
                except TimeoutError:
                    pass
                if self._stopping:
                    break
                target = str(getattr(self.brain, "model", self.settings.llm_model))
                resident = await self.brain.ready()
                if target != self.model or not resident:
                    if self.settings.ollama_preload:
                        # Re-check after acquiring the preload lock; an explicit
                        # API/model-change request may have recovered it already.
                        await self.preload(force=False)
                    else:
                        await self._set_state(OllamaReadiness.OLLAMA_OFFLINE, target)
                elif self.state != OllamaReadiness.OLLAMA_READY:
                    await self._set_state(OllamaReadiness.OLLAMA_READY, target)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                await self._set_state(OllamaReadiness.OLLAMA_ERROR, str(getattr(self.brain, "model", "")))
                await asyncio.sleep(min(5, self.settings.ollama_recovery_interval_seconds))

    async def _set_state(self, state: OllamaReadiness, model: str) -> None:
        changed = state != self.state or model != self.model
        self.state = state
        if changed:
            await self.event_bus.publish(EventType.OLLAMA_READINESS_CHANGED, state=state.value, model=model)

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "ready": self.state == OllamaReadiness.OLLAMA_READY,
            "model": self.model or str(getattr(self.brain, "model", self.settings.llm_model)),
            "keep_alive": self.settings.ollama_keep_alive,
            "preload": self.settings.ollama_preload,
            "warmup": self.settings.ollama_warmup,
            "last_error": self.last_error,
            "metrics": dict(self.metrics),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _ns_ms(value: Any) -> float:
        return round(float(value or 0) / 1_000_000, 1)
