from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TtsProviderStatus(StrEnum):
    LOCAL_READY = "LOCAL_READY"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    DISABLED = "DISABLED"
    READY = "READY"
    CONNECTING = "CONNECTING"
    STREAMING = "STREAMING"
    ERROR = "ERROR"
    DEGRADED = "DEGRADED"
    AUTH_ERROR = "AUTH_ERROR"
    QUOTA_ERROR = "QUOTA_ERROR"
    RATE_LIMIT_ERROR = "RATE_LIMIT_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    PROVIDER_ERROR = "PROVIDER_ERROR"


@dataclass(frozen=True, slots=True)
class ProviderValidation:
    valid: bool
    status: TtsProviderStatus
    reason: str | None = None


class TtsProviderError(RuntimeError):
    """Sanitized provider failure. Remote bodies and credentials stay private."""

    def __init__(self, status: TtsProviderStatus, message: str) -> None:
        self.status = status
        super().__init__(message)
