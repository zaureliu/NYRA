from __future__ import annotations

import re
import time
from typing import Any

import psutil

from app.intelligence.models import ModelRoute, RuntimeState


class ModelRouterV2:
    """Routes only among models reported by the live Ollama inventory."""

    def __init__(self, brain, *, preferences: dict[str, list[str]] | None = None) -> None:
        self.brain = brain
        self.preferences = preferences or {}
        self._inventory: dict[str, Any] = {}
        self._inventory_at = 0.0
        self.last_route: ModelRoute | None = None

    async def inventory(self, *, force: bool = False) -> dict[str, Any]:
        if not force and self._inventory and time.monotonic() - self._inventory_at < 15:
            return self._inventory
        value = await self.brain.inventory()
        models = []
        for item in value.get("models", []):
            name = str(item.get("name") or "")
            capabilities = self._capabilities(name, item)
            models.append({**item, "capabilities": sorted(capabilities), "healthy": bool(item.get("installed"))})
        self._inventory = {**value, "models": models}
        self._inventory_at = time.monotonic()
        return self._inventory

    async def route(self, request: str, *, context_characters: int = 0,
                    requested_capabilities: list[str] | None = None) -> ModelRoute:
        inventory = await self.inventory()
        task_type, required = self._classify(request, context_characters)
        required.update(requested_capabilities or [])
        available_memory = psutil.virtual_memory().available
        candidates: list[dict[str, Any]] = []
        for model in inventory.get("models", []):
            caps = set(model.get("capabilities") or [])
            score = 0.0
            if required.issubset(caps):
                score += 5
            else:
                score += len(required & caps) - len(required - caps) * 2
            name = str(model.get("name") or "")
            preferred = self.preferences.get(task_type, [])
            if name in preferred:
                score += 4 - preferred.index(name) * 0.25
            if name == inventory.get("official_model"):
                score += 1.2
            if model.get("loaded"):
                score += 1.0
            size = int(model.get("size") or 0)
            if size and size > available_memory * 0.72:
                score -= 5
            if task_type == "fast" and "fast" in caps:
                score += 2
            candidates.append({"model": name, "score": round(score, 4), "capabilities": sorted(caps), "loaded": model.get("loaded"), "size": size})
        candidates.sort(key=lambda item: item["score"], reverse=True)
        selected = candidates[0]["model"] if candidates else None
        fallbacks = [item["model"] for item in candidates[1:4] if item["model"]]
        route = ModelRoute(
            task_type=task_type, selected_model=selected, fallback_models=fallbacks,
            required_capabilities=sorted(required), context_characters=context_characters,
            reason="highest live capability/resource score" if selected else "no installed model available",
            inventory_state=RuntimeState.AVAILABLE if selected else RuntimeState.OFFLINE,
            resource_snapshot={"ram_available_bytes": available_memory, "ram_percent": psutil.virtual_memory().percent},
            candidates=candidates[:8],
        )
        self.last_route = route
        return route

    async def route_for_messages(self, messages) -> ModelRoute:
        text = "\n".join(str(getattr(item, "content", "")) for item in messages[-8:])
        return await self.route(text, context_characters=sum(len(str(getattr(item, "content", ""))) for item in messages))

    async def fallback_for(self, selected: str) -> str | None:
        if self.last_route and self.last_route.selected_model == selected:
            return next((name for name in self.last_route.fallback_models if name != selected), None)
        route = await self.route("fallback", context_characters=0)
        return next((name for name in [route.selected_model, *route.fallback_models] if name and name != selected), None)

    @staticmethod
    def _classify(text: str, context_characters: int) -> tuple[str, set[str]]:
        lowered = text.casefold()
        if re.search(r"\b(código|code|python|typescript|rust|bug|stack trace|refactor|teste)\b", lowered):
            return "coding", {"coding", "reasoning"}
        if re.search(r"\b(imagem|screenshot|visão|vision|foto)\b", lowered):
            return "vision", {"vision"}
        if context_characters > 20_000:
            return "long_context", {"long-context", "reasoning"}
        if re.search(r"\b(planej|analise|diagnóstico|compare|por que|causa|arquitetura)\b", lowered):
            return "reasoning", {"reasoning", "general"}
        if len(text) < 180:
            return "fast", {"fast", "general"}
        return "general", {"general"}

    @staticmethod
    def _capabilities(name: str, metadata: dict[str, Any]) -> set[str]:
        lowered = name.casefold()
        caps = {"general", "classification", "fallback"}
        if any(value in lowered for value in ("qwen3", "deepseek", "reason", "r1")):
            caps.update({"reasoning", "coding"})
        if any(value in lowered for value in ("coder", "code", "wrench")):
            caps.add("coding")
        if any(value in lowered for value in ("vision", "vl", "llava", "moondream")):
            caps.add("vision")
        if any(value in lowered for value in ("1b", "2b", "3b", "4b", "mini", "small")):
            caps.add("fast")
        else:
            caps.add("fast")  # latency ranking still prefers resident/smaller models
        context_length = int(metadata.get("context_length") or 0)
        if context_length >= 32_000 or "192k" in lowered or "128k" in lowered:
            caps.add("long-context")
        return caps
