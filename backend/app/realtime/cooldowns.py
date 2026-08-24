from __future__ import annotations

import time


class CooldownManager:
    def __init__(self) -> None:
        self._used: dict[str, float] = {}

    def ready(self, key: str, seconds: float, *, consume: bool = False) -> bool:
        now = time.monotonic()
        allowed = now - self._used.get(key, float("-inf")) >= seconds
        if allowed and consume:
            self._used[key] = now
        return allowed

    def consume(self, key: str) -> None:
        self._used[key] = time.monotonic()

    def remaining(self, key: str, seconds: float) -> float:
        return max(0.0, seconds - (time.monotonic() - self._used.get(key, float("-inf"))))

    def snapshot(self) -> dict[str, float]:
        now = time.monotonic()
        return {key: round(now - used, 1) for key, used in self._used.items()}
