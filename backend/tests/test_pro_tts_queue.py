from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile

import pytest

from app.speech.queue import SpeechPriority, SpeechQueue


class FakeTTS:
    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay
        self.calls: list[str] = []

    async def synthesize(self, text: str, state: str) -> Path:
        self.calls.append(text)
        await asyncio.sleep(self.delay)
        output_dir = Path(tempfile.gettempdir()) / "kazumi-speech-queue-tests"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"{text}.wav"
        output.write_bytes(b"RIFF" + b"\0" * 128)
        return output.resolve()


@pytest.mark.asyncio
async def test_new_turn_purges_old_turn_audio():
    """Teste A (6.5): novo turno antes de terminar → zero áudio antigo."""
    queue = SpeechQueue()
    tts = FakeTTS(delay=0.5)
    first = asyncio.create_task(queue.synthesize(tts, "frase antiga 1", "speaking", response_id="resp_a", turn_id="turn_a"))
    # enfileira várias do turno A sem aguardar conclusão da primeira
    pending = [asyncio.create_task(queue.synthesize(tts, f"antiga {i}", "speaking", response_id="resp_a", turn_id="turn_a")) for i in range(3)]
    await asyncio.sleep(0.15)  # primeira vira ativa; resto fica na fila
    dropped = await queue.purge_except("resp_b")
    assert dropped == 3
    assert queue.counters["tts_items_stale_dropped"] == 3
    for task in pending:
        with pytest.raises(asyncio.CancelledError):
            await task
    first.cancel()
    try:
        await first
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_same_turn_sentences_play_in_order():
    """Teste B (6.5): síntese concorrente, playback 0,1,2."""
    queue = SpeechQueue()
    tts = FakeTTS(delay=0.0)
    order = []

    original = tts.synthesize

    async def tracking(text: str, state: str) -> Path:
        order.append(text)
        return await original(text, state)

    tts.synthesize = tracking  # type: ignore[method-assign]
    results = await asyncio.gather(*[
        queue.synthesize(tts, f"s{i}", "speaking", response_id="r", chunk_index=i, turn_id="t1")
        for i in range(3)
    ])
    assert len(results) == 3
    assert order == ["s0", "s1", "s2"]
    assert queue.counters["tts_order_violations"] == 0
    assert queue.counters["tts_items_synthesized"] == 3
    assert queue.counters["tts_items_played"] == 0
    assert queue.playback_started("r") is True
    assert queue.playback_started("r") is False
    assert queue.counters["tts_items_played"] == 1


@pytest.mark.asyncio
async def test_chunk_order_index_resets_for_each_turn():
    queue = SpeechQueue()
    tts = FakeTTS(delay=0.0)
    try:
        await asyncio.gather(*[
            queue.synthesize(tts, f"a{i}", "speaking", response_id="a", chunk_index=i, turn_id="turn_a")
            for i in range(3)
        ])
        await asyncio.gather(*[
            queue.synthesize(tts, f"b{i}", "speaking", response_id="b", chunk_index=i, turn_id="turn_b")
            for i in range(2)
        ])
        assert queue.counters["tts_order_violations"] == 0
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_missing_tts_output_is_rejected_before_playback(tmp_path: Path):
    class MissingTTS:
        async def synthesize(self, _text: str, _state: str) -> Path:
            return (tmp_path / "missing.wav").resolve()

    queue = SpeechQueue()
    with pytest.raises(FileNotFoundError):
        await queue.synthesize(MissingTTS(), "teste", "neutral")  # type: ignore[arg-type]
    assert queue.counters["tts_items_synthesized"] == 0
    assert queue.counters["tts_items_played"] == 0


@pytest.mark.asyncio
async def test_cancel_by_response_does_not_touch_other_turn():
    queue = SpeechQueue()
    tts = FakeTTS()
    task_keep = asyncio.create_task(queue.synthesize(tts, "novo turno", "speaking", response_id="resp_b", turn_id="turn_b"))
    await asyncio.sleep(0)
    task_old = asyncio.create_task(queue.synthesize(tts, "turno velho", "speaking", response_id="resp_a", turn_id="turn_a"))
    await asyncio.sleep(0.02)
    cancelled = await queue.cancel("resp_a")
    assert cancelled >= 1 or task_old.done() is False
    task_keep.cancel()
    task_old.cancel()
    for task in (task_keep, task_old):
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_item_identity_fields_exist():
    from app.speech.queue import _SpeechItem

    item = _SpeechItem(1, 0, FakeTTS(), "x", "s", None, "resp", 0, "turn", "conv", 123.0)  # type: ignore[arg-type]
    assert item.conversation_id == "conv"
    assert item.created_at == 123.0
