from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


class RiskLevel(StrEnum):
    READ_ONLY = "READ_ONLY"
    LOW_RISK = "LOW_RISK"
    ELEVATED = "ELEVATED"
    DESTRUCTIVE = "DESTRUCTIVE"
    CRITICAL = "CRITICAL"
    # Kept for compatibility with the separate SkillRegistry permission model.
    CONFIRM_REQUIRED = "CONFIRM_REQUIRED"
    DANGEROUS = "DANGEROUS"


class EmptyInput(BaseModel):
    pass


class HostInput(BaseModel):
    host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9._:-]+$")
    timeout_seconds: float = Field(default=3, ge=0.2, le=15)


class PortInput(HostInput):
    port: int = Field(ge=1, le=65535)


class HttpInput(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    timeout_seconds: float = Field(default=5, ge=0.2, le=30)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Somente URLs HTTP/HTTPS completas são permitidas")
        if parsed.username or parsed.password:
            raise ValueError("Credenciais na URL não são permitidas")
        return value


class ToolResult(BaseModel):
    tool: str
    risk: RiskLevel
    ok: bool
    data: dict
    elapsed_ms: float


class NetworkWindowInput(BaseModel):
    minutes: int = Field(default=5, ge=1, le=15)


class NetworkHistoryInput(BaseModel):
    hours: int = Field(default=24, ge=1, le=720)
    limit: int = Field(default=50, ge=1, le=200)
