from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from app.selfdev.models import SelfDevSettings


class SelfDevScheduler:
    def __init__(
        self,
        settings: SelfDevSettings,
        run_once: Callable[[], Awaitable[Any]],
        safe_idle: Callable[[], bool | Awaitable[bool]],
        *,
        interval_seconds: float = 30,
    ) -> None:
        self.settings = settings
        self.run_once = run_once
        self.safe_idle = safe_idle
        self.interval_seconds = max(2, interval_seconds)
        self._task: asyncio.Task[None] | None = None
        self._day = date.today()
        self._promotions_today = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="kazumi-selfdev-scheduler")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def note_promotion(self) -> None:
        self._roll_day()
        self._promotions_today += 1

    def budget_available(self) -> bool:
        self._roll_day()
        return self._promotions_today < self.settings.max_auto_promotions_per_day

    def _roll_day(self) -> None:
        if self._day != date.today():
            self._day = date.today()
            self._promotions_today = 0

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            if not self.settings.run_when_idle or not self.budget_available():
                continue
            idle = self.safe_idle()
            if asyncio.iscoroutine(idle):
                idle = await idle
            if idle:
                try:
                    await self.run_once()
                except Exception:
                    continue
