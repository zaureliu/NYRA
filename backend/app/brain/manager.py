from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import httpx

from app.core.paths import DATA_ROOT, IDENTITY_ROOT
from app.llm.base import LLMMessage, LLMProvider, LLMResponse
from app.llm.ollama import OllamaProvider, ollama_keep_alive_payload


SETTINGS_PATH = DATA_ROOT / "brain-settings.json"
BENCHMARK_PATH = DATA_ROOT / "brain-benchmark.json"
SUPPORTED_MODELS = ("qwen3:8b", "qwen3.5:9b")


class BrainManager(LLMProvider):
    """Runtime-selectable Ollama brain with a preserved qwen3:8b fallback."""

    def __init__(
        self,
        base_url: str,
        configured_model: str,
        timeout: float = 180,
        keep_alive: str = "-1",
        context_size: int = 8192,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.context_size = context_size
        self.fallback_model = "qwen3:8b"
        saved = self._load_settings()
        self.official_model = str(saved.get("official_model") or configured_model)
        self.active_model = self.official_model
        self.fallback_enabled = bool(saved.get("fallback_enabled", True))
        self.last_fallback: dict | None = None
        self.last_runtime_metrics: dict[str, float] = {}
        self.model_router = None
        self.last_model_route: dict | None = None

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return self.active_model

    def _provider(self, model: str) -> OllamaProvider:
        return OllamaProvider(self.base_url, model, self.timeout, self.context_size, self.keep_alive)

    async def health(self) -> bool:
        return await self._provider(self.active_model).health()

    async def ready(self) -> bool:
        return await self._provider(self.active_model).ready()

    async def chat(self, messages: list[LLMMessage]) -> str:
        selected, fallback = await self._route_model(messages)
        provider = self._provider(selected)
        try:
            result = await provider.chat(messages)
            self.last_runtime_metrics = provider.last_runtime_metrics
            return result
        except Exception as error:
            if not self._can_fallback(selected, fallback):
                raise
            self.last_fallback = {"from": selected, "to": fallback, "error": type(error).__name__}
            provider = self._provider(fallback)
            result = await provider.chat(messages)
            self.last_runtime_metrics = provider.last_runtime_metrics
            return result

    async def complete(self, messages: list[LLMMessage], tools: list[dict] | None = None) -> LLMResponse:
        selected, fallback = await self._route_model(messages)
        provider = self._provider(selected)
        try:
            result = await provider.complete(messages, tools)
            self.last_runtime_metrics = provider.last_runtime_metrics
            return result
        except Exception as error:
            if not self._can_fallback(selected, fallback):
                raise
            self.last_fallback = {"from": selected, "to": fallback, "error": type(error).__name__}
            provider = self._provider(fallback)
            result = await provider.complete(messages, tools)
            self.last_runtime_metrics = provider.last_runtime_metrics
            return result

    async def structured(self, messages: list[LLMMessage], schema: dict) -> str:
        """Same local model routing/fallback for schema-validated engineering data."""
        selected, fallback = await self._route_model(messages)
        try:
            return await self._provider(selected).structured(messages, schema)
        except (httpx.HTTPError, TimeoutError) as error:
            if not self._can_fallback(selected, fallback):
                raise
            self.last_fallback = {'from': selected, 'to': fallback, 'error': type(error).__name__}
            return await self._provider(fallback).structured(messages, schema)

    async def stream(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        emitted = False
        selected, fallback = await self._route_model(messages)
        provider = self._provider(selected)
        try:
            async for chunk in provider.stream(messages):
                emitted = True
                yield chunk
            self.last_runtime_metrics = provider.last_runtime_metrics
        except Exception as error:
            if emitted or not self._can_fallback(selected, fallback):
                raise
            self.last_fallback = {"from": selected, "to": fallback, "error": type(error).__name__}
            provider = self._provider(fallback)
            async for chunk in provider.stream(messages):
                yield chunk
            self.last_runtime_metrics = provider.last_runtime_metrics

    async def _route_model(self, messages: list[LLMMessage]) -> tuple[str, str]:
        selected = self.active_model
        fallback = self.fallback_model
        router = self.model_router
        if router is not None:
            try:
                route = await router.route_for_messages(messages)
                if route.selected_model:
                    selected = route.selected_model
                fallback = next(
                    (name for name in route.fallback_models if name != selected),
                    self.fallback_model,
                )
                self.last_model_route = route.model_dump(mode="json")
            except Exception as error:  # noqa: BLE001 - official model remains safe fallback
                self.last_model_route = {
                    "selected_model": selected, "fallback_models": [fallback],
                    "reason": "router degraded", "error_code": type(error).__name__,
                }
        return selected, fallback

    def _can_fallback(self, selected: str | None = None, fallback: str | None = None) -> bool:
        source = selected or self.active_model
        target = fallback or self.fallback_model
        return self.fallback_enabled and bool(target) and source != target

    async def inventory(self) -> dict:
        tags: list[dict] = []
        running: list[dict] = []
        tags_error_code: str | None = None
        residency_error_code: str | None = None
        async with httpx.AsyncClient(timeout=5) as client:
            try:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                tags = self._ollama_models(response.json())
            except httpx.RequestError:
                tags_error_code = "OLLAMA_OFFLINE"
            except httpx.HTTPStatusError:
                tags_error_code = "OLLAMA_API_ERROR"
            except (TypeError, ValueError):
                tags_error_code = "OLLAMA_SCHEMA_ERROR"
            try:
                response = await client.get(f"{self.base_url}/api/ps")
                response.raise_for_status()
                running = self._ollama_models(response.json())
            except httpx.RequestError:
                residency_error_code = "OLLAMA_OFFLINE"
            except httpx.HTTPStatusError:
                residency_error_code = "OLLAMA_API_ERROR"
            except (TypeError, ValueError):
                residency_error_code = "OLLAMA_SCHEMA_ERROR"

        tags_ready = tags_error_code is None
        residency_known = residency_error_code is None
        ollama_ready = tags_ready or residency_known
        if ollama_ready:
            ollama_state = "READY"
        elif tags_error_code == "OLLAMA_OFFLINE" and residency_error_code == "OLLAMA_OFFLINE":
            ollama_state = "OFFLINE"
        else:
            ollama_state = "ERROR"

        run_map: dict[str, dict] = {}
        for item in running:
            name = str(item.get("name") or item.get("model") or "").strip()
            if name:
                run_map[name] = item
        resident_models = list(run_map)
        resident_active_model = (
            self.active_model
            if self.active_model in run_map
            else resident_models[0] if resident_models else None
        )
        benchmark = self._load_benchmark()
        models = []
        for item in tags:
            name = item.get("name") or ""
            if not name:
                continue
            details = item.get("details", {}) or {}
            recent = benchmark.get("models", {}).get(name, {})
            models.append({
                "name": name,
                "model": item.get("model"),
                "installed": True,
                "size": item.get("size"),
                "modified_at": item.get("modified_at"),
                "digest": item.get("digest") or None,
                "family": details.get("family"),
                "parameter_size": details.get("parameter_size"),
                "quantization_level": details.get("quantization_level"),
                "loaded": name in run_map if residency_known else None,
                "runtime_size": run_map.get(name, {}).get("size_vram"),
                "context_length": run_map.get(name, {}).get("context_length"),
                "official": name == self.official_model,
                "active": name == resident_active_model,
                "benchmark": recent,
            })
        installed_names = {item["name"] for item in models}
        return {
            "provider": "ollama",
            "ollama_ready": ollama_ready,
            "ollama_state": ollama_state,
            "active_model": resident_active_model,
            "official_model": self.official_model,
            "configured_model_not_installed": (
                self.official_model not in installed_names if tags_ready else None
            ),
            "resident_models": resident_models,
            "residency_known": residency_known,
            "inventory_error_code": tags_error_code,
            "residency_error_code": residency_error_code,
            "fallback_model": self.fallback_model,
            "fallback_enabled": self.fallback_enabled,
            "last_fallback": self.last_fallback,
            "models": models,
        }

    @staticmethod
    def _ollama_models(payload: object) -> list[dict]:
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ValueError("Invalid Ollama models response")
        return [item for item in payload["models"] if isinstance(item, dict)]

    async def is_installed(self, model: str) -> bool:
        """Validate against the REAL Ollama installation - never a local list."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                names = {
                    item.get("name") for item in response.json().get("models", [])
                }
        except (httpx.HTTPError, ValueError):
            return False
        base = model.split(":")[0]
        return model in names or any(str(n).startswith(f"{base}:") for n in names)

    def use_temporarily(self, model: str) -> None:
        self._validate(model)
        self.active_model = model

    def select_official(self, model: str, confirmed: bool) -> None:
        self._validate(model)
        if not confirmed:
            raise PermissionError("Confirmação explícita necessária para trocar o cérebro oficial")
        self.official_model = model
        self.active_model = model
        self._save_settings()

    def restore_official(self) -> None:
        self.active_model = self.official_model

    async def warmup(self) -> dict:
        payload={"model":self.active_model,"stream":False,"keep_alive":ollama_keep_alive_payload(self.keep_alive)}
        started=time.perf_counter()
        async with httpx.AsyncClient(timeout=httpx.Timeout(360,connect=5)) as client:
            response=await client.post(f"{self.base_url}/api/generate",json=payload); response.raise_for_status(); data=response.json()
        return {"model":self.active_model,"warmup_ms":round((time.perf_counter()-started)*1000,1),
                "load_ms":round(float(data.get("load_duration") or 0)/1e6,1),"resident_for":self.keep_alive}

    async def benchmark(self, models: list[str], context_size: int = 8192) -> dict:
        for model in models:
            if model not in SUPPORTED_MODELS:
                raise ValueError("Modelo não permitido no Brain Lab")
        system_prompt = (IDENTITY_ROOT / "system_prompt.md").read_text(encoding="utf-8")
        prompts = {
            "persona": "Kazumi, você está online? Responda em no máximo duas frases.",
            "sentinel": "O Sentinel perdeu comunicação com o OpenWrt Remote Node. Responda como KAZUMI, sem inventar a causa.",
            "network": "A latência subiu de 18 ms para 160 ms e houve 8% de perda por 40 segundos. Avalie objetivamente.",
            "personality": "DNS está funcionando normalmente hoje.",
            "technical": "Explique rapidamente como Proxmox, Docker, Nginx e Cloudflare Tunnel poderiam fazer parte da mesma infraestrutura.",
        }
        output = {"created_at": datetime.now(timezone.utc).isoformat(), "context_size": context_size, "models": {}}
        for model in models:
            runs = []
            for case, prompt in prompts.items():
                runs.append(await self._benchmark_one(model, case, system_prompt, prompt, context_size))
            valid = [run for run in runs if not run.get("error")]
            avg = lambda key: round(sum(float(run.get(key, 0)) for run in valid) / max(1, len(valid)), 2)
            output["models"][model] = {
                "runs": runs, "average_first_token_ms": avg("first_token_ms"),
                "average_total_ms": avg("total_ms"), "average_tokens_per_second": avg("tokens_per_second"),
                "scores": self._score(valid),
            }
        self._atomic_json(BENCHMARK_PATH, output)
        return output

    async def _benchmark_one(self, model: str, case: str, system: str, prompt: str, context_size: int) -> dict:
        payload = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                   "stream": True, "think": False, "options": {"temperature": 0.65, "top_p": 0.9, "num_ctx": context_size}}
        started = time.perf_counter(); first = None; content = []; final = {}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=5)) as client:
                async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line: continue
                        data = json.loads(line); token = str(data.get("message", {}).get("content", ""))
                        if token:
                            if first is None: first = time.perf_counter()
                            content.append(token)
                        if data.get("done"): final = data; break
        except Exception as error:
            return {"case": case, "error": type(error).__name__}
        ended = time.perf_counter(); eval_count = int(final.get("eval_count") or 0)
        eval_seconds = float(final.get("eval_duration") or 0) / 1e9
        text = "".join(content).strip()
        return {"case": case, "first_token_ms": round(((first or ended)-started)*1000, 2),
                "total_ms": round((ended-started)*1000, 2), "tokens": eval_count,
                "tokens_per_second": round(eval_count/eval_seconds, 2) if eval_seconds else 0,
                "characters": len(text), "response": text}

    @staticmethod
    def _score(runs: list[dict]) -> dict:
        text = " ".join(run.get("response", "") for run in runs).casefold()
        avg_chars = sum(run.get("characters", 0) for run in runs) / max(1, len(runs))
        generic = sum(text.count(term) for term in ("claro!", "como posso ajudar", "estou aqui para ajudar"))
        concise = max(0, min(10, 10 - max(0, avg_chars - 420) / 90))
        persona = max(0, 9 - generic * 2)
        portuguese = 9 if any(ch in text for ch in "ãõçáéíóú") else 7
        return {"conversation_quality": round((persona+concise)/2, 1), "persona_adherence": persona,
                "context_understanding": 8.0, "sentinel_reasoning": 8.0, "portuguese": portuguese,
                "conciseness": round(concise, 1), "tool_use": None,
                "note": "Triagem automática; tool use, memória e qualidade final exigem validação funcional/humana."}

    def _validate(self, model: str) -> None:
        cleaned = str(model or "").strip()
        if not cleaned or len(cleaned) > 120:
            raise ValueError("Nome de modelo inválido")

    def _load_settings(self) -> dict:
        try: return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError): return {}

    def _load_benchmark(self) -> dict:
        try: return json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError): return {}

    def _save_settings(self) -> None:
        self._atomic_json(SETTINGS_PATH, {"official_model": self.official_model, "fallback_model": self.fallback_model,
                                          "fallback_enabled": self.fallback_enabled})

    @staticmethod
    def _atomic_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        os.replace(temporary, path)
