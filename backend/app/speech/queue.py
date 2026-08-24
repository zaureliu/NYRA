from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from app.speech.tts import TTSProvider


class SpeechPriority(IntEnum):
    CRITICAL = 0
    USER = 1
    WARNING = 2
    INFORMATIONAL = 3


@dataclass(order=True)
class _SpeechItem:
    priority: int
    sequence: int
    provider: TTSProvider = field(compare=False)
    text: str = field(compare=False)
    state: str = field(compare=False)
    result: asyncio.Future[Path] = field(compare=False)
    response_id: str | None = field(default=None, compare=False)
    chunk_index: int | None = field(default=None, compare=False)
    turn_id: str | None = field(default=None, compare=False)


class SpeechQueue:
    """Serializes synthesis and provides cancellation/barge-in readiness.

    Items carry the owning turn_id so barge-in cancels only audio from old turns
    and never reuses or blocks speech belonging to a newer turn.
    """

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue[_SpeechItem] = asyncio.PriorityQueue()
        self._sequence = itertools.count()
        self._worker: asyncio.Task[None] | None = None
        self._active: asyncio.Task[Path] | None = None
        self._active_item: _SpeechItem | None = None

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="nyra-speech-queue")

    async def stop(self) -> None:
        await self.clear(cancel_active=True)
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    async def synthesize(
        self,
        provider: TTSProvider,
        text: str,
        state: str,
        priority: SpeechPriority = SpeechPriority.USER,
        response_id: str | None = None,
        chunk_index: int | None = None,
        turn_id: str | None = None,
    ) -> Path:
        self.start()
        loop = asyncio.get_running_loop()
        result: asyncio.Future[Path] = loop.create_future()
        await self._queue.put(
            _SpeechItem(int(priority), next(self._sequence), provider, text, state, result, response_id, chunk_index, turn_id)
        )
        return await result

    async def clear(self, cancel_active: bool = False) -> int:
        cleared = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not item.result.done():
                item.result.cancel()
            self._queue.task_done()
            cleared += 1
        if cancel_active and self._active and not self._active.done():
            self._active.cancel()
        return cleared

    async def cancel(self, response_id: str) -> int:
        """Cancel only queued speech that belongs to one realtime response/turn."""
        retained: list[_SpeechItem] = []
        cleared = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            if response_id and (item.response_id == response_id or item.turn_id == response_id):
                if not item.result.done():
                    item.result.cancel()
                cleared += 1
            else:
                retained.append(item)
        for item in retained:
            await self._queue.put(item)
        active_matches = bool(
            self._active_item
            and response_id
            and (self._active_item.response_id == response_id or self._active_item.turn_id == response_id)
        )
        if active_matches and self._active and not self._active.done():
            self._active.cancel()
            cleared += 1
        return cleared

    @property
    def pending(self) -> int:
        return self._queue.qsize() + int(bool(self._active and not self._active.done()))

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                self._active_item = item
                self._active = asyncio.create_task(
                    item.provider.synthesize(item.text, item.state),
                    name="nyra-tts-synthesis",
                )
                output = await self._active
                if not item.result.done():
                    item.result.set_result(Path(output))
            except asyncio.CancelledError:
                if not item.result.done():
                    item.result.cancel()
                if self._worker and asyncio.current_task() is self._worker:
                    raise
            except Exception as exc:
                if not item.result.done():
                    item.result.set_exception(exc)
            finally:
                self._active = None
                self._active_item = None
                self._queue.task_done()
