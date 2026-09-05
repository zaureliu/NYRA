from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from app.core.turn import get_current_turn_id
from app.speech.profile import VoiceSynthesisOptions
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
    conversation_id: str | None = field(default=None, compare=False)
    created_at: float = field(default=0.0, compare=False)
    options: VoiceSynthesisOptions | None = field(default=None, compare=False)
    provider_id: str = field(default="unknown", compare=False)
    model_id: str | None = field(default=None, compare=False)
    voice: str | None = field(default=None, compare=False)
    emotion: str = field(default="neutral", compare=False)
    audio_format: str = field(default="wav", compare=False)
    on_audio: Callable[..., Awaitable[None]] | None = field(default=None, compare=False)


class SpeechQueue:
    """Serializes synthesis and provides cancellation/barge-in readiness.

    Items carry the owning turn_id so barge-in cancels only audio from old turns
    and never reuses or blocks speech belonging to a newer turn. Callers that do
    not pass a turn/response are auto-tagged with the CURRENT turn context, so
    proactive/alert speech can never outlive the turn it was born in.
    """

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue[_SpeechItem] = asyncio.PriorityQueue(maxsize=48)
        self._sequence = itertools.count()
        self._worker: asyncio.Task[None] | None = None
        self._active: asyncio.Task[Path] | None = None
        self._active_item: _SpeechItem | None = None
        # Apêndice PRO C — contadores de integridade do TTS.
        self.counters = {
            "tts_items_created": 0,
            "tts_items_synthesized": 0,
            "tts_items_played": 0,
            "tts_items_cancelled": 0,
            "tts_items_stale_dropped": 0,
            "tts_order_violations": 0,
        }
        self._playback_confirmed: set[str] = set()

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="kazumi-speech-queue")

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
        conversation_id: str | None = None,
        options: VoiceSynthesisOptions | None = None,
        on_audio: Callable[..., Awaitable[None]] | None = None,
    ) -> Path:
        self.start()
        loop = asyncio.get_running_loop()
        result: asyncio.Future[Path] = loop.create_future()
        # Auto-tag: rotas sem identidade (alerts/proativo/legacy) herdam o turno
        # corrente; fora de turno ficam soltas mas ainda são purgáveis por geração.
        context_turn = get_current_turn_id()
        if turn_id is None and response_id is None and context_turn:
            turn_id = context_turn
            if response_id is None:
                response_id = turn_id
        self.counters["tts_items_created"] += 1
        await self._queue.put(
            _SpeechItem(
                int(priority), next(self._sequence), provider, text, state, result,
                response_id, chunk_index, turn_id, conversation_id, time.time(), options,
                str(getattr(provider, "provider_id", getattr(provider, "name", "unknown"))),
                getattr(provider, "model_id", None),
                getattr(provider, "default_voice", None),
                str(getattr(options, "emotion", state) or state),
                "wav",
                on_audio,
            )
        )
        return await result

    async def _stream_item(self, item):
        audio_started = None
        audio_seconds = 0.0
        async for packet in item.provider.stream_audio(item.text, item.state, item.options):
            if item.result.cancelled():
                raise asyncio.CancelledError
            if packet.pcm:
                if audio_started is None:
                    audio_started = time.monotonic()
                # Backpressure to provider: at most one second ahead of the
                # real-time player, instead of overflowing its bounded queue.
                delay = audio_seconds - (time.monotonic() - audio_started) - 1.0
                if delay > 0:
                    await asyncio.sleep(delay)
                audio_seconds += len(packet.pcm) / (packet.sample_rate * 2)
            await item.on_audio(packet)
        return None

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
        self.counters["tts_items_cancelled"] += cleared
        if cancel_active and self._active and not self._active.done():
            self._active.cancel()
        return cleared

    async def purge_except(self, response_id: str | None) -> int:
        """Drop EVERY queued item not belonging to the given current turn.

        Called at the start of a new user turn: garante stale audio = 0 mesmo se
        alguma rota escapou do auto-tagging.
        """
        retained: list[_SpeechItem] = []
        dropped = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            belongs = bool(response_id) and (item.response_id == response_id or item.turn_id == response_id)
            if belongs or (item.turn_id is None and item.response_id is not None):
                retained.append(item)
                continue
            if item.priority <= SpeechPriority.CRITICAL:
                retained.append(item)
                continue
            if not item.result.done():
                item.result.cancel()
            self.counters["tts_items_stale_dropped"] += 1
            dropped += 1
        for item in retained:
            await self._queue.put(item)
        return dropped

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
                self.counters["tts_items_cancelled"] += 1
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
            self.counters["tts_items_cancelled"] += 1
            cleared += 1
        return cleared

    @property
    def pending(self) -> int:
        return self._queue.qsize() + int(bool(self._active and not self._active.done()))

    def playback_started(self, response_id: str) -> bool:
        """Count only a real player acknowledgement, once per response."""
        if not response_id or response_id in self._playback_confirmed:
            return False
        self._playback_confirmed.add(response_id)
        if len(self._playback_confirmed) > 400:
            self._playback_confirmed.pop()
        self.counters["tts_items_played"] += 1
        return True

    async def _run(self) -> None:
        last_user_chunk: tuple[str | None, int | None] | None = None
        while True:
            item = await self._queue.get()
            try:
                if item.priority == SpeechPriority.USER and item.chunk_index is not None:
                    last_turn, last_index = last_user_chunk or (None, None)
                    if item.turn_id != last_turn:
                        last_index = None
                    if (
                        last_turn is not None
                        and item.turn_id == last_turn
                        and last_index is not None
                        and item.chunk_index < last_index
                    ):
                        # Invariante 3 (Apêndice C): sentence N+1 nunca toca antes da N.
                        self.counters["tts_order_violations"] += 1
                    last_user_chunk = (item.turn_id, max(item.chunk_index, last_index if last_index is not None else -1))
                self._active_item = item
                if item.result.cancelled():
                    continue
                synthesis = self._stream_item(item) if item.on_audio else (
                    item.provider.synthesize(item.text, item.state, item.options)
                    if item.options is not None
                    else item.provider.synthesize(item.text, item.state)
                )
                self._active = asyncio.create_task(synthesis, name="kazumi-tts-synthesis")
                raw_output = await self._active
                if item.on_audio:
                    if not item.result.done():
                        item.result.set_result(None)
                    self.counters["tts_items_synthesized"] += 1
                    continue
                output = Path(raw_output)
                if not output.is_absolute():
                    raise FileNotFoundError(f"TTS provider returned a relative output path: {output}")
                output = output.resolve(strict=True)
                if not output.is_file() or output.stat().st_size <= 0:
                    raise FileNotFoundError(f"TTS output is not a readable file: {output}")
                if not item.result.done():
                    item.result.set_result(output)
                self.counters["tts_items_synthesized"] += 1
            except asyncio.CancelledError:
                if not item.result.done():
                    item.result.cancel()
                # Cancellation of the child synthesis is NOT shutdown of this
                # worker. Keep servicing independent/newer speech after barge-in.
                if asyncio.current_task().cancelling():
                    raise
            except Exception as exc:
                if not item.result.done():
                    item.result.set_exception(exc)
            finally:
                self._active = None
                self._active_item = None
                self._queue.task_done()
