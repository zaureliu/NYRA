from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import StrEnum
import time
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

from app.tools.grounding import VerificationStatus


class AgentRunState(StrEnum):
    OBSERVE = "OBSERVE"
    DIAGNOSE = "DIAGNOSE"
    PLAN = "PLAN"
    ACT = "ACT"
    VERIFY = "VERIFY"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    CANCELLED = "CANCELLED"


class AgentRunStatus(StrEnum):
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_UNVERIFIED_ACTION = "COMPLETED_WITH_UNVERIFIED_ACTION"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AgentStep(BaseModel):
    index: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    state: AgentRunState
    tool: str
    target: str = "local"
    operation: str = ""
    command_fingerprint: str
    result_fingerprint: str
    success: bool
    risk_level: str
    verification_status: VerificationStatus = VerificationStatus.NOT_REQUIRED
    tool_call_id: str | None = None
    execution_id: str | None = None
    summary: str = ""


class AgentRun(BaseModel):
    id: str
    goal: str
    turn_id: str | None = None
    conversation_id: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    state: AgentRunState = AgentRunState.OBSERVE
    status: AgentRunStatus = AgentRunStatus.RUNNING
    steps: list[AgentStep] = Field(default_factory=list)
    reasoning_steps: int = 0
    tool_calls: int = 0
    host_targets: list[str] = Field(default_factory=list)
    pending_approval_id: str | None = None
    final_summary: str = ""
    error: str = ""


TransitionCallback = Callable[[AgentRunState], Awaitable[None]]
StepCallback = Callable[[str, dict, dict, dict], Awaitable[None]]
LockCallback = Callable[[str], Awaitable[bool]]
UnlockCallback = Callable[[str], Awaitable[None]]


class AgentLoopRuntime:
    def __init__(
        self,
        run: AgentRun,
        *,
        max_steps: int,
        max_tool_calls: int,
        max_runtime_seconds: int,
        max_identical_repeats: int,
        max_consecutive_failures: int,
        read_only: bool,
        cancellation: asyncio.Event,
        transition: TransitionCallback,
        record_step: StepCallback,
        acquire_resource: LockCallback,
        release_resource: UnlockCallback,
    ) -> None:
        self.run = run
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.max_runtime_seconds = max_runtime_seconds
        self.max_identical_repeats = max_identical_repeats
        self.max_consecutive_failures = max_consecutive_failures
        self.read_only = read_only
        self.cancellation = cancellation
        self.transition = transition
        self.record_step = record_step
        self.acquire_resource = acquire_resource
        self.release_resource = release_resource
        self.started_monotonic = time.monotonic()
        self.needs_verification = False
        self.unverified_action = False
        self.pending_approval_id: str | None = None
        self.stop_reason: str | None = None
        self.held_resources: set[str] = set()
        self.required_remote_host: str | None = None
        self.required_remote_address: str | None = None
        self.remote_attempted = False
        self.required_local_backend = False
        self.local_backend_port: int | None = None
        self.local_backend_root: str | None = None
        self.local_backend_observations = 0

    def expired(self) -> bool:
        return time.monotonic() - self.started_monotonic >= self.max_runtime_seconds

    def cancelled(self) -> bool:
        return self.cancellation.is_set()
