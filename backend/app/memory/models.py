from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class MemoryCategory(StrEnum):
    SHORT_TERM = "short_term"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PREFERENCES = "preferences"
    HOMELAB_EVENTS = "homelab_events"


class MemoryCreate(BaseModel):
    category: MemoryCategory
    content: str = Field(min_length=1, max_length=4000)
    importance: int = Field(default=5, ge=1, le=10)
    role: str | None = Field(default=None, pattern=r"^(user|assistant|system)?$")
    metadata: dict = Field(default_factory=dict)


class MemoryRecord(BaseModel):
    id: int
    category: MemoryCategory
    content: str
    importance: int
    role: str | None = None
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

