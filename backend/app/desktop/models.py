"""NYRA Desktop Application Control V1 - models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class LaunchErrorCode(StrEnum):
    UNKNOWN_APP = "UNKNOWN_APP"
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    EXECUTABLE_NOT_FOUND = "EXECUTABLE_NOT_FOUND"
    SPAWN_FAILED = "SPAWN_FAILED"
    WINDOW_NOT_CONFIRMED = "WINDOW_NOT_CONFIRMED"
    ALREADY_OPEN = "ALREADY_OPEN"


class DesktopAppSpec(BaseModel):
    """One registered desktop application. Executables come ONLY from this trusted registry."""

    id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_]+$")
    display_name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    executable: str = Field(min_length=1, max_length=500)
    arguments: list[str] = Field(default_factory=list)
    working_directory: str | None = None
    process_names: list[str] = Field(default_factory=list)
    window_title_contains: list[str] = Field(default_factory=list)
    single_instance: bool = False
    startup_timeout_seconds: float = Field(8.0, ge=1.0, le=60)
    risk: str = "LOW_RISK"
    notes: str = ""

    def normalized_process_names(self) -> list[str]:
        names = [name.casefold() for name in self.process_names]
        if not names:
            exe = self.executable.replace("/", "\\").rsplit("\\", 1)[-1].casefold()
            names = [exe, exe.removesuffix(".exe")]
        return names


class WindowInfo(BaseModel):
    hwnd: int
    pid: int
    title: str
    visible: bool
    process_name: str | None = None
    window_class: str | None = None


def operation_result(
    *,
    success: bool,
    app: str,
    action: str,
    error_code: str | None = None,
    message: str = "",
    duration_ms: float = 0,
    execution_success: bool | None = None,
    effect_verified: bool | None = None,
    verification_status: str = "NOT_REQUIRED",
    detail: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": success,
        "app": app,
        "action": action,
        "error_code": error_code,
        "message": message,
        "duration_ms": round(duration_ms, 1),
        "execution_success": execution_success,
        "effect_verified": effect_verified,
        "verification_status": verification_status,
    }
    if detail:
        payload.update(detail)
    payload.update(extra)
    return payload
