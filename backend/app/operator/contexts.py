"""Unified Operator Context layer (spec Parte N, §240-§250).

Five context kinds with strict separation:
    TurnContext     — conversational turn (lives in app.core.turn, untouched).
    TaskContext     — long-running multi-step task.
    JobContext      — persistent OS process job.
    WatchContext    — event watch registration.
    WorkflowContext — saved human workflow execution.

Rule §246: the five are never mixed. Rule §248: every tool call records which
context it belongs to. Rule §250: cross-context use of a context id is
rejected (CrossContextRejectionError), mirroring CROSS_TURN_OBSERVATION_REJECTED.

No chain-of-thought is stored here — operational descriptors only (§140).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ContextKind(StrEnum):
    TURN = "TURN"
    TASK = "TASK"
    JOB = "JOB"
    WATCH = "WATCH"
    WORKFLOW = "WORKFLOW"


class CrossContextRejectionError(Exception):
    """Raised when a context id is used outside its registered kind/scope (§250)."""

    def __init__(self, requested_id: str, expected_kind: str, found_kind: str | None) -> None:
        self.requested_id = requested_id
        self.expected_kind = expected_kind
        self.found_kind = found_kind
        super().__init__(
            f"CROSS_CONTEXT_REJECTED: id={requested_id} esperado {expected_kind}, "
            f"encontrado {found_kind or 'inexistente'}"
        )


class OperatorContext(BaseModel):
    """Common envelope for operator contexts (id + correlation)."""

    context_id: str = Field(default_factory=lambda: uuid4().hex)
    kind: ContextKind
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime | None = None
    parent_turn_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or _utcnow()) >= self.expires_at


class TaskContext(OperatorContext):
    """Long task context (§242): resources and progress live here, not in turn memory."""

    kind: ContextKind = ContextKind.TASK
    goal: str = ""
    state: str = "PLANNING"
    deadline: datetime | None = None


class JobContext(OperatorContext):
    """Persistent process context (§243): pid identity survives API restarts."""

    kind: ContextKind = ContextKind.JOB
    name: str = ""
    job_type: str = "process"
    pid: int | None = None
    create_time: float | None = None


class WatchContext(OperatorContext):
    """Event-watch context (§244): TTL-scoped by design (§180)."""

    kind: ContextKind = ContextKind.WATCH
    event_types: list[str] = Field(default_factory=list)


class WorkflowContext(OperatorContext):
    """Workflow execution context (§245)."""

    kind: ContextKind = ContextKind.WORKFLOW
    workflow_id: str = ""
    workflow_version: int = 1


class OperatorContextRegistry:
    """Registry + correlation guard for the five operator contexts.

    The TurnContext keeps living inside TurnRegistry; this registry only tracks
    the four NEW kinds plus provides lookup/validation helpers so tool calls can
    prove which context they belong to (§248) and misuse fails closed (§250).
    """

    def __init__(self, max_active: int = 256) -> None:
        self._items: dict[str, OperatorContext] = {}
        self._max_active = max_active
        self.metrics = {"registered": 0, "expired": 0, "cross_context_rejections": 0}

    def register(self, context: OperatorContext) -> OperatorContext:
        self._sweep()
        if len(self._items) >= self._max_active and context.context_id not in self._items:
            self._drop_oldest()
        self._items[context.context_id] = context
        self.metrics["registered"] += 1
        return context

    def unregister(self, context_id: str) -> bool:
        return self._items.pop(context_id, None) is not None

    def get(self, context_id: str, expected_kind: ContextKind | None = None) -> OperatorContext:
        item = self._items.get(context_id)
        if item is not None and item.expired():
            self._items.pop(context_id, None)
            self.metrics["expired"] += 1
            item = None
        if item is None:
            raise KeyError(context_id)
        if expected_kind is not None and item.kind != expected_kind:
            self.metrics["cross_context_rejections"] += 1
            raise CrossContextRejectionError(context_id, expected_kind.value, item.kind.value)
        return item

    def find(self, kind: ContextKind) -> list[OperatorContext]:
        self._sweep()
        return [item for item in self._items.values() if item.kind == kind]

    def count(self, kind: ContextKind) -> int:
        return len(self.find(kind))

    def snapshot(self) -> dict[str, Any]:
        self._sweep()
        counts = {kind.value: 0 for kind in ContextKind}
        for item in self._items.values():
            counts[item.kind.value] += 1
        return {"counts": counts, "metrics": dict(self.metrics)}

    # ------------------------------------------------------------------ internals
    def _sweep(self) -> None:
        expired = [key for key, item in self._items.items() if item.expired()]
        for key in expired:
            self._items.pop(key, None)
            self.metrics["expired"] += 1

    def _drop_oldest(self) -> None:
        if not self._items:
            return
        oldest_key = min(self._items, key=lambda key: self._items[key].created_at)
        self._items.pop(oldest_key, None)
