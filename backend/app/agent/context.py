from __future__ import annotations

from contextvars import ContextVar


current_agent_run_id: ContextVar[str | None] = ContextVar("nyra_agent_run_id", default=None)
