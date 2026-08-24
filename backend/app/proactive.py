from __future__ import annotations

from collections import deque
import time

from app.realtime.cooldowns import CooldownManager


class ProactiveEngine:
    def __init__(self, cooldowns: CooldownManager, max_low_priority_per_hour: int = 2) -> None:
        self.cooldowns = cooldowns
        self.max_low_priority_per_hour = max_low_priority_per_hour
        self.quiet_mode = False
        self._low_priority: deque[float] = deque()

    def allow(self, key: str, *, priority: int, cooldown_seconds: float, user_busy: bool = False) -> bool:
        if priority >= 90:
            return True
        if self.quiet_mode or user_busy or not self.cooldowns.ready(key, cooldown_seconds):
            return False
        now = time.monotonic()
        while self._low_priority and now - self._low_priority[0] >= 3600:
            self._low_priority.popleft()
        if priority < 70 and len(self._low_priority) >= self.max_low_priority_per_hour:
            return False
        self.cooldowns.consume(key)
        if priority < 70:
            self._low_priority.append(now)
        return True

    def status(self) -> dict:
        return {"quiet_mode": self.quiet_mode, "low_priority_last_hour": len(self._low_priority), "budget": self.max_low_priority_per_hour}
