from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.parse import urlparse

from app.llm.base import LLMMessage
from app.llm.ollama import OllamaProvider
from app.selfdev.models import PatchBundle, SelfDevPlan, TaskComplexity


class BrainInventory(Protocol):
    async def inventory(self) -> dict[str, Any]: ...


class SelfDevModelRouter:
    """Uses only locally installed Ollama models and accepts strict JSON patches."""

    def __init__(
        self,
        brain: BrainInventory,
        *,
        base_url: str,
        model: str = "qwen3:8b",
        timeout: float = 180,
        context_size: int = 8192,
        keep_alive: str = "1h",
    ) -> None:
        self.brain = brain
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.context_size = context_size
        self.keep_alive = keep_alive

    async def installed_models(self) -> list[str]:
        self._require_local_endpoint()
        inventory = await self.brain.inventory()
        return [str(item.get("name")) for item in inventory.get("models", []) if item.get("installed") and item.get("name")]

    async def validate_model(self, model: str | None = None) -> bool:
        selected = model or self.model
        return selected in await self.installed_models()

    @staticmethod
    def auto_implementation_allowed(complexity: TaskComplexity) -> bool:
        return complexity in {TaskComplexity.TRIVIAL, TaskComplexity.SMALL, TaskComplexity.MEDIUM}

    async def propose_patch(self, plan: SelfDevPlan, repository_context: dict[str, Any]) -> PatchBundle:
        self._require_local_endpoint()
        if not self.auto_implementation_allowed(plan.complexity):
            raise PermissionError("TASK_TOO_COMPLEX_FOR_AUTONOMOUS_PATCH")
        if not await self.validate_model():
            raise RuntimeError("SELFDEV_MODEL_NOT_INSTALLED")
        provider = OllamaProvider(
            self.base_url, self.model, self.timeout, self.context_size, self.keep_alive
        )
        schema = PatchBundle.model_json_schema()
        messages = [
            LLMMessage(
                role="system",
                content=(
                    "Você é o worker local do KAZUMI SelfDev. Responda SOMENTE JSON válido conforme o schema. "
                    "Nunca gere comandos de shell, secrets, deleções ou paths absolutos. "
                    "Cada UPDATE exige expected_sha256 do índice fornecido."
                ),
            ),
            LLMMessage(
                role="user",
                content=json.dumps({
                    "plan": plan.model_dump(mode="json"),
                    "repository_context": repository_context,
                    "output_schema": schema,
                }, ensure_ascii=False),
            ),
        ]
        raw = await provider.chat(messages)
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise ValueError("SELFDEV_MODEL_INVALID_JSON") from error
        return PatchBundle.model_validate(value)

    def _require_local_endpoint(self) -> None:
        host = (urlparse(self.base_url).hostname or "").casefold()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise PermissionError("SELFDEV_LOCAL_MODEL_REQUIRED")
