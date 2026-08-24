from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SkillPermission(StrEnum):
    READ_ONLY = "READ_ONLY"
    CONFIRM_REQUIRED = "CONFIRM_REQUIRED"
    DANGEROUS = "DANGEROUS"


class SkillDefinition(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    description: str
    triggers: list[str] = Field(default_factory=list)
    permission: SkillPermission = SkillPermission.READ_ONLY
    cooldown_seconds: float = Field(0, ge=0, le=86400)
    priority: int = Field(40, ge=0, le=100)
    enabled: bool = True
    available: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillResult(BaseModel):
    name: str
    ok: bool
    permission: SkillPermission
    data: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: float
