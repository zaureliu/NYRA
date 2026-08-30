from __future__ import annotations

import time
from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BudgetLimits:
    max_tool_calls: int = 20
    max_retries: int = 3
    max_planner_iterations: int = 12
    max_consecutive_failures: int = 3
    timeout_seconds: float = 300
    max_restarts: int = 2
    destructive_actions: int = 0
    network_actions: int = 10


@dataclass
class ActionBudget:
    limits: BudgetLimits
    started_at: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    retries: int = 0
    planner_iterations: int = 0
    consecutive_failures: int = 0
    restarts: int = 0
    destructive_actions: int = 0
    network_actions: int = 0

    def consume(self, kind: str, amount: int = 1) -> None:
        if time.monotonic() - self.started_at > self.limits.timeout_seconds:
            raise BudgetExceeded("ACTION_BUDGET_TIMEOUT")
        attribute = {
            "tool": "tool_calls", "retry": "retries", "planner": "planner_iterations",
            "failure": "consecutive_failures", "restart": "restarts",
            "destructive": "destructive_actions", "network": "network_actions",
        }.get(kind)
        if not attribute:
            raise ValueError("unknown budget kind")
        setattr(self, attribute, getattr(self, attribute) + amount)
        maximum = {
            "tool": self.limits.max_tool_calls, "retry": self.limits.max_retries,
            "planner": self.limits.max_planner_iterations, "failure": self.limits.max_consecutive_failures,
            "restart": self.limits.max_restarts, "destructive": self.limits.destructive_actions,
            "network": self.limits.network_actions,
        }[kind]
        if getattr(self, attribute) > maximum:
            raise BudgetExceeded(f"ACTION_BUDGET_{kind.upper()}_EXCEEDED")

    def success(self) -> None:
        self.consecutive_failures = 0

    def snapshot(self) -> dict:
        return {"elapsed_seconds": round(time.monotonic() - self.started_at, 3),
                "tool_calls": self.tool_calls, "retries": self.retries,
                "planner_iterations": self.planner_iterations,
                "consecutive_failures": self.consecutive_failures,
                "restarts": self.restarts, "destructive_actions": self.destructive_actions,
                "network_actions": self.network_actions}
