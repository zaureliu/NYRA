"""Public schemas for NYRA's grounded, local World State Engine."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class WorldFreshness(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    EXPIRED = "EXPIRED"
    PERSISTENT = "PERSISTENT"


class WorldValue(BaseModel):
    """One observation with provenance; an expired value is never exposed."""

    value: Any = None
    source: str
    observed_at: datetime
    confidence: float = Field(ge=0.0, le=1.0)
    freshness: WorldFreshness
    verified: bool


class WorldEvent(BaseModel):
    event_type: str
    summary: str
    source: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    verified: bool = True


class WorldSnapshot(BaseModel):
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    current_app: WorldValue | None = None
    current_window: WorldValue | None = None
    current_process: WorldValue | None = None
    current_desktop_target: WorldValue | None = None
    current_browser: WorldValue | None = None
    current_url: WorldValue | None = None
    current_tab: WorldValue | None = None
    current_project: WorldValue | None = None
    current_file: WorldValue | None = None
    recent_files: WorldValue | None = None
    current_task: WorldValue | None = None
    current_operation: WorldValue | None = None
    recent_apps: WorldValue | None = None
    recent_artifacts: WorldValue | None = None
    active_monitors: WorldValue | None = None
    active_tasks: WorldValue | None = None
    active_goal: WorldValue | None = None
    open_loop_count: WorldValue | None = None
    waiting_loop_count: WorldValue | None = None
    most_relevant_open_loop: WorldValue | None = None
    connected_usb: WorldValue | None = None
    hardware_activity: WorldValue | None = None
    recent_hardware_events: WorldValue | None = None
    network_state: WorldValue | None = None
    integration_state: dict[str, WorldValue | None] = Field(default_factory=dict)
    conversation_state: WorldValue | None = None
    current_focus: WorldValue | None = None
    recent_events: list[WorldEvent] = Field(default_factory=list)
    user_activity_state: WorldValue | None = None
    assistant_state: WorldValue | None = None
    nyra_emotion: WorldValue | None = None
    dialogue_policy: WorldValue | None = None
