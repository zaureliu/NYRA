import asyncio

import pytest

from app.core.config import Settings
from app.events import Event, EventBus, EventType
from app.api.routes import _voice_satellite_event_allowed
from app.listening import AlwaysListeningManager
from app.listening.wake_word import TranscriptWakeWordProvider


def test_voice_satellite_only_receives_actionable_owned_backend_errors():
    unrelated = Event(type=EventType.ERROR, payload={"operation": "always_listening", "error": "NoSuchFile"})
    actionable = Event(type=EventType.ERROR, payload={
        "operation": "satellite_capture",
        "satellite_id": "desktop-satellite-01",
        "satellite_action_required": True,
    })
    assert _voice_satellite_event_allowed(unrelated, None) is True
    assert _voice_satellite_event_allowed(unrelated, "desktop-satellite-01") is False
    assert _voice_satellite_event_allowed(actionable, "desktop-satellite-01") is True
    assert _voice_satellite_event_allowed(actionable, "another-satellite") is False


def test_local_wake_word_extracts_command_and_rejects_mentions():
    provider = TranscriptWakeWordProvider()
    match = provider.detect("Nyra, como está a rede?", "Nyra")
    assert match.detected is True
    assert match.command_text == "como esta a rede?"
    assert provider.detect("Eu estava falando sobre a Nyra ontem", "Nyra").detected is False


@pytest.mark.asyncio
async def test_wake_word_opens_hands_free_and_close_phrase_ends_it(tmp_path):
    settings = Settings.from_sources(
        database_path=tmp_path / "memory.db",
        always_listening_enabled=True,
        listening_mode="wake_word",
        wake_word="Nyra",
        hands_free_timeout_seconds=120,
    )
    manager = AlwaysListeningManager(settings, EventBus())
    first = manager.decide("Nyra, como está a rede?")
    assert first.accepted and first.wake_word_detected
    follow_up = manager.decide("E o servidor?")
    assert follow_up.accepted and follow_up.reason == "hands_free"
    closed = manager.decide("Pode ficar quieta agora")
    assert closed.close_session and not closed.accepted
    assert manager.decide("E a rede?").reason == "wake_word_absent"


@pytest.mark.asyncio
async def test_mute_releases_capture_and_tts_guard_blocks_self_voice(tmp_path):
    settings = Settings.from_sources(
        database_path=tmp_path / "memory.db",
        always_listening_enabled=True,
        voice_barge_in=False,
    )
    bus = EventBus()
    manager = AlwaysListeningManager(settings, bus)
    await manager.start()
    assert await manager.acquire_lease("client_12345678")
    assert manager.can_process("client_12345678")[0]
    await bus.publish(EventType.TTS_STARTED)
    assert manager.can_process("client_12345678")[1] == "self_voice_guard"
    await manager.set_muted(True)
    assert manager.status()["microphone"] is False
    assert manager.can_process("client_12345678")[1] == "muted"
    await manager.stop()


@pytest.mark.asyncio
async def test_only_one_capture_client_holds_lease(tmp_path):
    settings = Settings.from_sources(
        database_path=tmp_path / "memory.db",
        always_listening_enabled=True,
        voice_barge_in=False,
    )
    manager = AlwaysListeningManager(settings, EventBus())
    assert await manager.acquire_lease("desktop_12345678")
    assert not await manager.acquire_lease("dashboard_12345678")
    assert manager.owns_lease("desktop_12345678")


@pytest.mark.asyncio
async def test_stale_playback_guard_recovers_without_reopening_microphone_during_tts(tmp_path):
    settings = Settings.from_sources(
        database_path=tmp_path / "memory.db",
        always_listening_enabled=True,
        voice_barge_in=False,
    )
    manager = AlwaysListeningManager(settings, EventBus())
    assert await manager.acquire_lease("desktop_12345678")
    await manager.playback(True)
    assert manager.can_process("desktop_12345678")[1] == "self_voice_guard"
    manager._speaking_until = 1.0
    assert manager.status()["speaking_guard"] is True
    manager._guard_until = 0.0
    assert manager.can_process("desktop_12345678") == (True, "ready")


@pytest.mark.asyncio
async def test_barge_in_keeps_capture_eligible_during_playback(tmp_path):
    settings = Settings.from_sources(
        database_path=tmp_path / "memory.db",
        always_listening_enabled=True,
        voice_barge_in=True,
    )
    manager = AlwaysListeningManager(settings, EventBus())
    assert await manager.acquire_lease("desktop_12345678")
    await manager.playback(True)
    assert manager.can_process("desktop_12345678") == (True, "ready")
