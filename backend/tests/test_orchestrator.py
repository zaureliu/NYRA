from pathlib import Path

import pytest

from app.character import StateMachine
from app.events import EventBus, EventType
from app.llm.mock import MockLLMProvider
from app.memory import MemoryRepository
from app.orchestrator import ChatOrchestrator
from app.speech.tts import DisabledTTS


@pytest.mark.asyncio
async def test_mock_llm_pipeline_persists_both_messages(tmp_path: Path):
    bus = EventBus()
    memory = MemoryRepository(tmp_path / "nyra.db", bus)
    await memory.initialize()
    orchestrator = ChatOrchestrator(
        MockLLMProvider("Estou online. Os serviços parecem comportados, por enquanto."),
        memory,
        StateMachine(memory, bus),
        bus,
        DisabledTTS(),
    )
    result = await orchestrator.converse("Nyra, você está online?", synthesize=False)
    conversation = await memory.recent_conversation()
    assert result.response.startswith("Estou online")
    assert result.display_text == result.response
    assert result.speech_text
    assert result.speech_text != ""
    assert [item.role for item in conversation] == ["user", "assistant"]
    assert EventType.NYRA_RESPONSE in [event.type for event in bus.history()]
