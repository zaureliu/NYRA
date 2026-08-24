from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.core.runtime_settings import save_runtime_settings
from app.events import Event, EventBus, EventType
from app.listening.models import ListeningMode, ListeningSettingsUpdate, UtteranceDecision
from app.listening.wake_word import TranscriptWakeWordProvider, WakeWordProvider


logger = logging.getLogger("nyra.listening")
PLAYBACK_SAFETY_SECONDS = 60.0
CLOSE_SESSION = re.compile(
    r"\b(pode parar de ouvir|encerra (?:a )?conversa|ate depois|pode ficar quieta agora)\b",
    re.IGNORECASE,
)


@dataclass
class _Lease:
    client_id: str
    expires_at: float


class AlwaysListeningManager:
    """Controls consent, wake-word sessions and self-voice suppression.

    Audio capture lives in the trusted UI process.  This service receives only
    complete VAD-gated utterances, never a permanent microphone stream.
    """

    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        wake_word: WakeWordProvider | None = None,
    ) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.wake_word = wake_word or TranscriptWakeWordProvider()
        self.enabled = settings.always_listening_enabled
        self.muted = False
        self.processing = False
        self.speaking = False
        self._speaking_until = 0.0
        self._hands_free_until = 0.0
        self._guard_until = 0.0
        self._lease: _Lease | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self.event_bus.subscribe(self.handle_event)
        await self._publish_status()

    async def stop(self) -> None:
        await self.event_bus.unsubscribe(self.handle_event)
        self.enabled = False
        self._lease = None

    async def handle_event(self, event: Event) -> None:
        if event.type == EventType.TTS_STARTED:
            self.speaking = True
            self._speaking_until = time.monotonic() + PLAYBACK_SAFETY_SECONDS
        elif event.type == EventType.TTS_FINISHED:
            self.speaking = False
            self._speaking_until = 0.0
            self._guard_until = time.monotonic() + self.settings.listening_guard_ms / 1000
        elif event.type == EventType.SPEECH_CANCELLED:
            self.speaking = False
            self._speaking_until = 0.0
            self._guard_until = 0
        else:
            return
        await self._publish_status()

    def config(self) -> ListeningSettingsUpdate:
        return ListeningSettingsUpdate(
            enabled=self.enabled,
            mode=self.settings.listening_mode,
            wake_word=self.settings.wake_word,
            hands_free_timeout_seconds=self.settings.hands_free_timeout_seconds,
            vad_threshold=self.settings.vad_threshold,
            energy_threshold=self.settings.silence_threshold,
            preroll_ms=self.settings.mic_preroll_ms,
            postroll_ms=self.settings.mic_postroll_ms,
            speech_start_ms=self.settings.voice_speech_start_ms,
            max_utterance_seconds=self.settings.voice_max_utterance_seconds,
            guard_ms=self.settings.listening_guard_ms,
            microphone=self.settings.microphone,
            barge_in=self.settings.voice_barge_in,
            audio_debug=self.settings.listening_audio_debug,
            privacy_indicator=self.settings.listening_privacy_indicator,
        )

    async def update(self, value: ListeningSettingsUpdate) -> dict[str, Any]:
        self.enabled = value.enabled
        if not self.enabled:
            self._hands_free_until = 0
            self._lease = None
        updates = {
            "always_listening_enabled": value.enabled,
            "listening_mode": value.mode.value,
            "wake_word": value.wake_word,
            "hands_free_timeout_seconds": value.hands_free_timeout_seconds,
            "vad_threshold": value.vad_threshold,
            "silence_threshold": value.energy_threshold,
            "mic_preroll_ms": value.preroll_ms,
            "mic_postroll_ms": value.postroll_ms,
            "voice_speech_start_ms": value.speech_start_ms,
            "voice_max_utterance_seconds": value.max_utterance_seconds,
            "listening_guard_ms": value.guard_ms,
            "microphone": value.microphone,
            "voice_barge_in": value.barge_in,
            "listening_audio_debug": value.audio_debug,
            "listening_privacy_indicator": value.privacy_indicator,
        }
        for key, item in updates.items():
            setattr(self.settings, key, item)
        await asyncio.to_thread(save_runtime_settings, updates)
        await self.event_bus.publish(EventType.LISTENING_SETTINGS_CHANGED, **self.status())
        return self.status()

    async def set_enabled(self, enabled: bool) -> dict[str, Any]:
        value = self.config().model_copy(update={"enabled": enabled})
        return await self.update(value)

    async def set_muted(self, muted: bool) -> dict[str, Any]:
        self.muted = muted
        if muted:
            self._lease = None
            self._hands_free_until = 0
        await self.event_bus.publish(EventType.MICROPHONE_STATE_CHANGED, **self.status())
        return self.status()

    async def acquire_lease(self, client_id: str, ttl_seconds: int = 15) -> bool:
        async with self._lock:
            now = time.monotonic()
            if not self.enabled or self.muted:
                return False
            if self._lease and self._lease.expires_at > now and self._lease.client_id != client_id:
                return False
            self._lease = _Lease(client_id, now + ttl_seconds)
            return True

    def owns_lease(self, client_id: str) -> bool:
        return bool(
            self._lease
            and self._lease.client_id == client_id
            and self._lease.expires_at > time.monotonic()
        )

    async def playback(self, playing: bool) -> dict[str, Any]:
        self.speaking = playing
        if playing:
            self._speaking_until = time.monotonic() + PLAYBACK_SAFETY_SECONDS
        else:
            self._speaking_until = 0.0
            self._guard_until = time.monotonic() + self.settings.listening_guard_ms / 1000
        await self._publish_status()
        return self.status()

    def can_process(self, client_id: str) -> tuple[bool, str]:
        if not self.enabled:
            return False, "disabled"
        if self.muted:
            return False, "muted"
        if not self.owns_lease(client_id):
            return False, "lease_required"
        if self.processing:
            return False, "busy"
        if self._is_speaking() and not self.settings.voice_barge_in:
            return False, "self_voice_guard"
        if time.monotonic() < self._guard_until:
            return False, "guard_interval"
        return True, "ready"

    def decide(self, transcript: str) -> UtteranceDecision:
        text = " ".join(transcript.strip().split())
        if not text:
            return UtteranceDecision(accepted=False, reason="empty")
        now = time.monotonic()
        hands_free = now < self._hands_free_until
        mode = ListeningMode(self.settings.listening_mode)
        if CLOSE_SESSION.search(text.casefold()):
            self._hands_free_until = 0
            return UtteranceDecision(
                accepted=False,
                reason="session_closed",
                close_session=True,
            )
        if mode == ListeningMode.PUSH_TO_TALK:
            return UtteranceDecision(accepted=True, reason="push_to_talk", text=text)
        match = self.wake_word.detect(text, self.settings.wake_word)
        if match.detected:
            self._hands_free_until = now + self.settings.hands_free_timeout_seconds
            if not match.command_text:
                return UtteranceDecision(
                    accepted=False,
                    reason="wake_only",
                    wake_word_detected=True,
                    hands_free_active=True,
                )
            return UtteranceDecision(
                accepted=True,
                reason="wake_word",
                text=match.command_text,
                wake_word_detected=True,
                hands_free_active=True,
            )
        if mode == ListeningMode.HANDS_FREE or hands_free:
            self._hands_free_until = now + self.settings.hands_free_timeout_seconds
            return UtteranceDecision(
                accepted=True,
                reason="hands_free",
                text=text,
                hands_free_active=True,
            )
        return UtteranceDecision(accepted=False, reason="wake_word_absent")

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        remaining = max(0, round(self._hands_free_until - now, 1))
        lease_active = bool(self._lease and self._lease.expires_at > now)
        speaking = self._is_speaking()
        speaking_remaining = max(0, round(self._speaking_until - now, 1)) if speaking else 0
        guard_remaining = max(0, round(self._guard_until - now, 1))
        return {
            "enabled": self.enabled,
            "muted": self.muted,
            "microphone": self.enabled and not self.muted and lease_active,
            "processing": self.processing,
            "speaking_guard": speaking or guard_remaining > 0,
            "speaking": speaking,
            "speaking_remaining_seconds": speaking_remaining,
            "guard_remaining_seconds": guard_remaining,
            "mode": self.settings.listening_mode,
            "wake_word": self.settings.wake_word,
            "wake_word_provider": self.wake_word.name,
            "hands_free_active": remaining > 0,
            "hands_free_remaining_seconds": remaining,
            "privacy_indicator": self.settings.listening_privacy_indicator,
            "lease_active": lease_active,
        }

    def _is_speaking(self) -> bool:
        if self.speaking and self._speaking_until and time.monotonic() >= self._speaking_until:
            self.speaking = False
            self._speaking_until = 0.0
            self._guard_until = max(
                self._guard_until,
                time.monotonic() + self.settings.listening_guard_ms / 1000,
            )
            logger.warning("playback_guard_safety_release")
        return self.speaking

    async def _publish_status(self) -> None:
        await self.event_bus.publish(EventType.MICROPHONE_STATE_CHANGED, **self.status())
