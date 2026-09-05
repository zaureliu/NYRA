"""Turn isolation primitives: immutable turn identity, ephemeral TurnContext.

Every operator input starts exactly one turn. The turn_id is created once at the
API boundary and propagated through the whole pipeline (ConversationEngine,
RealtimeOrchestrator, AgentController, ToolAgentLoop, GroundingLedger, shells,
desktop control, TTS, events). Per-turn mutable state lives exclusively inside
the TurnContext instance; singletons may hold services, never turn state.

Cross-turn enforcement: observations recorded under turn A can never be consumed
by turn B even if cleanup failed, because every lookup requires the matching
turn_id and mismatches are rejected with CROSS_TURN_OBSERVATION_REJECTED.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


TURN_ID_PREFIX = "turn_"
CROSS_TURN_OBSERVATION_REJECTED = "CROSS_TURN_OBSERVATION_REJECTED"


class TurnStatus(StrEnum):
    RUNNING = "RUNNING"
    TEXT_COMPLETE = "TEXT_COMPLETE"
    AUDIO_DEGRADED = "AUDIO_DEGRADED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TurnContext:
    """Ephemeral per-input state. One instance per user input, never reused."""

    __slots__ = (
        "turn_id", "conversation_id", "user_input", "created_at",
        "content_buffer", "thinking_buffer", "tool_buffer",
        "assistant_buffer", "pending_tool_calls",
        "tool_calls", "tool_results", "observations",
        "agent_run_id", "grounding_ledger",
        "tts_queue", "final_response", "status", "error",
        "approval_capable",
        "_finished",
    )

    def __init__(
        self,
        user_input: str = "",
        *,
        conversation_id: str = "default",
        turn_id: str | None = None,
        approval_capable: bool = True,
    ) -> None:
        self.turn_id = turn_id or new_turn_id()
        self.conversation_id = conversation_id
        self.user_input = user_input
        self.approval_capable = bool(approval_capable)
        self.created_at = datetime.now(timezone.utc)
        self.content_buffer: list[str] = []
        self.thinking_buffer: list[str] = []
        self.tool_buffer: list[str] = []
        self.assistant_buffer = ""
        self.pending_tool_calls: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.tool_results: list[dict[str, Any]] = []
        self.observations: list[str] = []
        self.agent_run_id: str | None = None
        self.grounding_ledger: Any = None
        self.tts_queue: list[dict[str, Any]] = []
        self.final_response: str | None = None
        self.status: TurnStatus = TurnStatus.RUNNING
        self.error: TurnError | None = None
        self._finished = False

    @property
    def response_id(self) -> str:
        """Backwards-compatible alias: streaming events keep using response_id."""
        return self.turn_id[len(TURN_ID_PREFIX):]

    def append_content(self, delta: str) -> None:
        self.content_buffer.append(delta)

    def content(self) -> str:
        return "".join(self.content_buffer)

    def finish(
        self,
        status: TurnStatus,
        *,
        final_response: str | None = None,
        error: "TurnError | None" = None,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        self.status = status
        if final_response is not None:
            self.final_response = final_response
            self.assistant_buffer = final_response
        if error is not None:
            self.error = error
        self.pending_tool_calls.clear()

    def cleanup(self) -> None:
        """Release references held by the turn; called on every terminal status."""
        self.content_buffer.clear()
        self.thinking_buffer.clear()
        self.tool_buffer.clear()
        self.pending_tool_calls.clear()
        self.tool_calls.clear()
        self.tool_results.clear()
        self.tts_queue.clear()
        self.grounding_ledger = None

    def debug_trace(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at.isoformat(),
            "status": self.status.value,
            "agent_run_id": self.agent_run_id,
            "tool_calls": len(self.tool_calls),
        }


class TurnError(BaseModel):
    """Structured pipeline error: stage-aware, never a bare RuntimeError."""

    stage: str
    error_code: str
    exception_type: str
    message: str = ""
    recoverable: bool = True
    turn_id: str | None = None


class PipelineFailure(Exception):
    """Carries a TurnError through the pipeline without losing the traceback."""

    def __init__(self, error: TurnError) -> None:
        super().__init__(error.message or error.error_code)
        self.error = error


def new_turn_id() -> str:
    return f"{TURN_ID_PREFIX}{uuid4().hex}"


def is_turn_id(value: str) -> bool:
    return isinstance(value, str) and value.startswith(TURN_ID_PREFIX) and len(value) > len(TURN_ID_PREFIX)


_current_turn_id: ContextVar[str | None] = ContextVar("kazumi_turn_id", default=None)
#: Public ContextVar carrying the active turn id for tools/events inside one turn.
current_turn_id: ContextVar[str | None] = _current_turn_id


def get_current_turn_id() -> str | None:
    return _current_turn_id.get()


def set_current_turn_id(turn_id: str | None) -> Token[str | None]:
    return _current_turn_id.set(turn_id)


def reset_current_turn_id(token: Token[str | None]) -> None:
    _current_turn_id.reset(token)


class TurnMetrics(BaseModel):
    active_turns: int = 0
    completed_turns: int = 0
    failed_turns: int = 0
    cancelled_turns: int = 0
    degraded_audio_turns: int = 0
    cross_turn_rejections: int = 0
    late_events_dropped: int = 0


class CrossTurnObservationError(Exception):
    """Raised when an observation from another turn is requested by design."""

    def __init__(self, observation_turn_id: str | None, requested_turn_id: str | None, tool_call_id: str) -> None:
        super().__init__(CROSS_TURN_OBSERVATION_REJECTED)
        self.error_code = CROSS_TURN_OBSERVATION_REJECTED
        self.observation_turn_id = observation_turn_id
        self.requested_turn_id = requested_turn_id
        self.tool_call_id = tool_call_id


class TurnRegistry:
    """Tracks active turns and lifetime metrics. Service-level singleton that
    holds only turn-scoped entries, each removed at terminal status."""

    def __init__(self, max_active: int = 64) -> None:
        self.max_active = max_active
        self._active: dict[str, TurnContext] = {}
        self.metrics = TurnMetrics()
        self.recent: list[dict[str, Any]] = []

    def start(self, turn: TurnContext) -> TurnContext:
        if len(self._active) >= self.max_active:
            oldest = next(iter(self._active))
            self.finish(oldest, TurnStatus.FAILED, error=TurnError(
                stage="turn_registry", error_code="TURN_OVERFLOW",
                exception_type="TurnOverflow", recoverable=False,
            ))
        self._active[turn.turn_id] = turn
        self.metrics.active_turns = len(self._active)
        return turn

    def get(self, turn_id: str | None) -> TurnContext | None:
        return self._active.get(turn_id or "")

    def finish(
        self,
        turn_id: str,
        status: TurnStatus,
        *,
        final_response: str | None = None,
        error: TurnError | None = None,
    ) -> TurnContext | None:
        turn = self._active.pop(turn_id, None)
        self.metrics.active_turns = len(self._active)
        if turn is None:
            return None
        turn.finish(status, final_response=final_response, error=error)
        turn.cleanup()
        if status == TurnStatus.COMPLETE:
            self.metrics.completed_turns += 1
        elif status == TurnStatus.CANCELLED:
            self.metrics.cancelled_turns += 1
        elif status == TurnStatus.AUDIO_DEGRADED:
            self.metrics.degraded_audio_turns += 1
            self.metrics.completed_turns += 1
        elif status == TurnStatus.TEXT_COMPLETE:
            self.metrics.completed_turns += 1
        else:
            self.metrics.failed_turns += 1
        self.recent.append({
            "turn_id": turn.turn_id,
            "conversation_id": turn.conversation_id,
            "status": status.value,
            "error": error.model_dump() if error else None,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })
        del self.recent[:-200]
        return turn

    def record_cross_turn_rejection(self, *, turn_id: str | None = None, detail: str = "") -> None:
        self.metrics.cross_turn_rejections += 1

    def record_late_event(self, *, turn_id: str | None = None) -> None:
        self.metrics.late_events_dropped += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics.model_dump(),
            "active": [context.debug_trace() for context in self._active.values()],
            "recent": self.recent[-20:],
        }
