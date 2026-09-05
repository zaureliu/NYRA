import pytest

from app.events import EventBus, EventType


@pytest.mark.asyncio
async def test_event_bus_notifies_and_keeps_history():
    bus = EventBus()
    received = []

    async def subscriber(event):
        received.append(event)

    await bus.subscribe(subscriber)
    event = await bus.publish(EventType.LLM_PROCESSING, state="focused")
    assert received == [event]
    assert bus.history()[-1].payload["state"] == "focused"
    await bus.unsubscribe(subscriber)



@pytest.mark.asyncio
async def test_slow_subscriber_does_not_block_publisher():
    """Parte 5.2: assinante lento não pode monopolizar o event loop."""
    import asyncio
    import time as _time

    from app.events import EventBus, EventType

    bus = EventBus()
    finished = []

    async def slow(event):
        await asyncio.sleep(5)
        finished.append(event.seq)

    async def fast(event):
        finished.append(f"fast-{event.seq}")

    await bus.subscribe(slow)
    await bus.subscribe(fast)
    started = _time.perf_counter()
    await asyncio.wait_for(
        bus.publish(EventType.KAZUMI_RESPONSE, text="x"),
        timeout=1.0,
    )
    elapsed = _time.perf_counter() - started
    assert elapsed < 1.0, "publisher bloqueado por assinante lento"
    assert bus.stats()["subscriber_timeouts"] == 1
    assert "fast-1" in finished
    detached = bus.detached_handler_tasks()
    if detached:
        await asyncio.wait(detached, timeout=6)
    await bus.unsubscribe(slow)
    await bus.unsubscribe(fast)


@pytest.mark.asyncio
async def test_publish_keeps_monotonic_sequence_and_counters():
    from app.events import EventBus, EventType

    bus = EventBus()
    await bus.publish(EventType.LLM_PROCESSING)
    await bus.publish(EventType.LLM_PROCESSING)
    history = bus.history()
    assert [e.seq for e in history] == [1, 2]
    assert bus.stats()["events_published"] == 2
