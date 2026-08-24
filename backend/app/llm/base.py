from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class LLMToolFunction(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("arguments", mode="before")
    @classmethod
    def parse_arguments(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            import json
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        raise ValueError("Tool arguments must be an object")


class LLMToolCall(BaseModel):
    function: LLMToolFunction
    tool_call_id: str | None = None


class LLMMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[LLMToolCall] = Field(default_factory=list)
    tool_name: str | None = None
    tool_call_id: str | None = None


class LLMResponse(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str = ""
    tool_calls: list[LLMToolCall] = Field(default_factory=list)

    def as_message(self) -> LLMMessage:
        return LLMMessage(role="assistant", content=self.content, tool_calls=self.tool_calls)


class LLMProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def chat(self, messages: list[LLMMessage]) -> str: ...

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """Structured completion hook used by native local tool calling."""

        return LLMResponse(content=await self.chat(messages))

    async def stream(self, messages: list[LLMMessage]) -> AsyncIterator[str]:
        """Compatibility stream for providers without native token streaming."""
        value = await self.chat(messages)
        if value:
            yield value

    @abstractmethod
    async def health(self) -> bool: ...

    async def ready(self) -> bool:
        """Whether this provider can answer now without a deferred model load."""
        return await self.health()
