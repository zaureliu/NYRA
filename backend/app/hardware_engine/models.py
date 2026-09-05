from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def now():
    return datetime.now(timezone.utc).isoformat()


class HardwareError(RuntimeError):
    def __init__(self, code: str, detail: str = ''):
        self.code, self.detail = code, detail
        super().__init__(code)


class GoalIntent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    effect: Literal['info', 'research', 'project', 'led_on', 'led_off', 'led_blink',
                    'button', 'display', 'sensor', 'web_server', 'resume', 'build', 'modify'] = 'info'
    target: str = Field(default='', max_length=100)
    interval_ms: int = Field(default=2000, ge=100, le=60000)
    text: str = Field(default='', max_length=1000)
    project_only: bool = False
    source: Literal['user_claim'] = 'user_claim'


class HardwareGoal(BaseModel):
    goal_id: str = Field(default_factory=lambda: 'hw_' + uuid4().hex)
    user_intent: GoalIntent
    target_device: dict = Field(default_factory=dict)
    target_project: str | None = None
    desired_effect: str
    constraints: dict = Field(default_factory=dict)
    plan: list[str] = Field(default_factory=list)
    steps: list[dict] = Field(default_factory=list)
    state: str = 'PLANNING'
    evidence: list[dict] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    sources: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    started_at: str = Field(default_factory=now)
    finished_at: str | None = None
    task_id: str | None = None
    turn_id: str | None = None
    response: str = ''
    simulated: bool = False
    plan_revision: dict = Field(default_factory=dict)
    loop_id: str | None = None


class BoardProfile(BaseModel):
    board_id: str
    name: str
    family: str
    chip: str
    platform: str
    framework: str = 'arduino'
    docs_url: str
    definition_url: str
    led_pin: int | None = None
    led_active_high: bool = True
    led_source: str | None = None
    sources: list[dict] = Field(default_factory=list)
