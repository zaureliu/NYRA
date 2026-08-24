from __future__ import annotations

from pydantic import BaseModel, Field


class PronunciationRule(BaseModel):
    canonical: str
    aliases: list[str] = Field(default_factory=list)
    category: str = "general"
    strategy: str = "spoken_alias"
    spoken_form: str | None = None
    provider_overrides: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    priority: int = 0
    notes: str | None = None


class PronunciationResult(BaseModel):
    original_text: str
    normalized_text: str
    speech_text: str
    applied_rules: list[dict] = Field(default_factory=list)
    detected_terms: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PronunciationDictionary(BaseModel):
    version: int = 1
    rules: list[PronunciationRule] = Field(default_factory=list)
