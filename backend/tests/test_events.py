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

