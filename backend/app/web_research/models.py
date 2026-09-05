from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=3, max_length=500)
    kind: Literal['research', 'official_docs', 'datasheet', 'repository', 'library'] = 'research'
    fresh: bool = True
    limit: int = Field(default=3, ge=1, le=5)


class Source(BaseModel):
    url: str
    title: str = ''
    source_type: str = 'community'
    retrieved_at: str = Field(default_factory=now)
    content_hash: str = ''
    relevance: float = 0
    facts: list[str] = Field(default_factory=list)
    text: str = ''
    links: list[dict[str, str]] = Field(default_factory=list)
    cached: bool = False
    stale: bool = False
    trust: Literal['WEB_CONTENT'] = 'WEB_CONTENT'
