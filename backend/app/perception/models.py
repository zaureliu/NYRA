from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class ForegroundApp(BaseModel):
    process: str = "unknown"
    classification: str = "Unknown"
    title: str | None = None


class MouseState(BaseModel):
    activity: Literal["recent", "still", "unknown"] = "unknown"
    relative_x: float | None = Field(None, ge=-1, le=1)
    relative_y: float | None = Field(None, ge=-1, le=1)
    monitor: int | None = None


class SystemSnapshot(BaseModel):
    cpu_percent: float = 0
    ram_percent: float = 0
    disk_percent: float = 0
    gpu_percent: float | None = None
    resolution: str | None = None
    audio_output_active: bool | None = None


class PerceptionSnapshot(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    enabled: bool = False
    user_activity: Literal["active", "idle", "long_idle", "unknown"] = "unknown"
    idle_seconds: float = 0
    foreground_app: ForegroundApp = Field(default_factory=ForegroundApp)
    mouse: MouseState = Field(default_factory=MouseState)
    system: SystemSnapshot = Field(default_factory=SystemSnapshot)
    network: dict = Field(default_factory=dict)
    sentinel: dict = Field(default_factory=dict)
    kazumi_state: str = "IDLE"
