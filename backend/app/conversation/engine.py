from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import time
from typing import Any
from uuid import uuid4

from app.conversation.models import AudioSettingsUpdate, ConversationState, InterruptionTarget
from app.core.runtime_settings import save_runtime_settings
from app.core.turn import TurnContext, new_turn_id
from app.events import Event, EventBus, EventType


TTSSwitcher = Callable[[str, str, float], Awaitable[dict[str, Any]]]


class ConversationEngine:
    """Single coordinator for voice turn state, STT, conversation and speech interruption.

    Browser capture owns device I/O; this component owns the backend lifecycle.  It
    deliberately separates cancelling generated speech from cancelling an Agent Run.
    """

    def __init__(self, settings, event_bus: EventBus, stt, listening, orchestrator, telemetry, warm_manager=None) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.stt = stt
        self.listening = listening
        self.orchestrator = orchestrator
        self.telemetry = telemetry
        self.warm_manager = warm_manager
        self.state = ConversationState.IDLE
        self._tts_switcher: TTSSwitcher | None = None
        self._lock = asyncio.Lock()
        self._last_error: str | None = None

    async def start(self) -> None:
        await self.event_bus.subscribe(self._observe_runtime_event)

    async def stop(self) -> None:
        await self.event_bus.unsubscribe(self._observe_runtime_event)

    def bind_tts_switcher(self, switcher: TTSSwitcher) -> None:
        self._tts_switcher = switcher

    async def speech_started(self, source: str = "microphone") -> bool:
        interrupted = False
        if self.state == ConversationState.SPEAKING and self.settings.voice_barge_in:
            interrupted = await self.orchestrator.cancel_speech("vad_barge_in")
        await self.event_bus.publish(
            EventType.USER_SPEECH_STARTED,
            source=source,
            barge_in=interrupted,
        )
        await self.transition(ConversationState.USER_SPEAKING, source=source, barge_in=interrupted)
        return interrupted

    async def transcribe(self, path, *, turn_id: str | None = None, speech_end: float | None = None):
        response_id = turn_id or new_turn_id()
        self.telemetry.start(response_id, speech_end=speech_end or time.perf_counter())
        self.telemetry.mark(response_id, "t_vad_end")
        await self.transition(ConversationState.TRANSCRIBING, response_id=response_id)
        self.telemetry.mark(response_id, "t_stt_start")
        await self.event_bus.publish(EventType.STT_STARTED, response_id=response_id, turn_id=response_id)
        started = time.perf_counter()
        result = await self.stt.transcribe(path)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        self.telemetry.mark(response_id, "t_stt_complete")
        self.telemetry.mark(response_id, "t_stt_final")
        await self.event_bus.publish(
            EventType.STT_COMPLETED,
            response_id=response_id,
            turn_id=response_id,
            duration_ms=elapsed_ms,
            empty=not bool(result.text.strip()),
        )
        if not result.text.strip():
            await self.transition(ConversationState.LISTENING, response_id=response_id, reason="empty_transcription")
        return response_id, result

    async def direct_audio_turn(self, path, *, speech_end: float | None = None):
        async with self._lock:
            response_id, transcription = await self.transcribe(path, speech_end=speech_end)
            if not transcription.text.strip():
                return {"accepted": False, "reason": "empty_transcription", "transcription": transcription.model_dump(mode="json")}
            turn = TurnContext(transcription.text, conversation_id="voice")
            if turn.turn_id != response_id and response_id.startswith("turn_"):
                turn.turn_id = response_id
            chat = await self.orchestrator.converse(
                transcription.text,
                synthesize=True,
                speech_end=speech_end,
                response_id=response_id,
                turn=turn,
            )
            return {
                "accepted": True,
                "reason": "push_to_talk",
                "turn_id": turn.turn_id,
                "transcription": transcription.model_dump(mode="json"),
                "chat": chat.model_dump(mode="json"),
            }

    async def listening_audio_turn(self, path, client_id: str, *, speech_end: float | None = None):
        async with self._lock:
            allowed, reason = self.listening.can_process(client_id)
            if not allowed:
                return {"accepted": False, "reason": reason, "status": self.listening.status()}
            self.listening.processing = True
            try:
                response_id, transcription = await self.transcribe(path, speech_end=speech_end)
                if not transcription.text.strip():
                    return {
                        "accepted": False,
                        "reason": "empty_transcription",
                        "transcription": transcription.model_dump(mode="json"),
                    }
                decision = self.listening.decide(transcription.text)
                if decision.wake_word_detected:
                    await self.event_bus.publish(
                        EventType.WAKE_WORD_DETECTED,
                        wake_word=self.settings.wake_word,
                        hands_free=decision.hands_free_active,
                    )
                    await self.event_bus.publish(EventType.HANDS_FREE_STARTED)
                if decision.close_session:
                    await self.event_bus.publish(EventType.HANDS_FREE_ENDED, reason="voice_command")
                if not decision.accepted:
                    await self.transition(ConversationState.LISTENING, response_id=response_id, reason=decision.reason)
                    return {
                        "accepted": False,
                        "reason": decision.reason,
                        "transcription": transcription.model_dump(mode="json"),
                        "decision": decision.model_dump(mode="json"),
                    }
                turn = TurnContext(decision.text, conversation_id="always_listening")
                if turn.turn_id != response_id and response_id.startswith("turn_"):
                    turn.turn_id = response_id
                chat = await self.orchestrator.converse(
                    decision.text,
                    synthesize=True,
                    speech_end=speech_end,
                    response_id=response_id,
                    turn=turn,
                )
                return {
                    "accepted": True,
                    "reason": decision.reason,
                    "turn_id": turn.turn_id,
                    "transcription": transcription.model_dump(mode="json"),
                    "decision": decision.model_dump(mode="json"),
                    "chat": chat.model_dump(mode="json"),
                }
            except Exception as exc:
                self._last_error = type(exc).__name__
                await self.transition(ConversationState.ERROR, error=self._last_error)
                raise
            finally:
                self.listening.processing = False

    async def interrupt(self, target: InterruptionTarget, reason: str = "operator") -> dict[str, Any]:
        speech_cancelled = await self.orchestrator.cancel_speech(reason)
        task_cancelled = False
        if target == InterruptionTarget.TASK and self.orchestrator.agent is not None:
            task_cancelled = await self.orchestrator.agent.cancel_active(reason)
        await self.transition(ConversationState.INTERRUPTED, target=target.value)
        await self.transition(ConversationState.LISTENING)
        return {"speech_cancelled": speech_cancelled, "task_cancelled": task_cancelled, "target": target.value}

    def audio_settings(self) -> dict[str, Any]:
        return {
            "microphone": self.settings.microphone,
            "speaker": self.settings.speaker,
            "voice": self.settings.tts_voice,
            "speech_speed": self.settings.tts_speaking_rate,
            "volume": self.settings.audio_volume,
            "conversation_mode": self.settings.listening_mode,
            "always_listening": self.listening.enabled,
            "allow_interruption": self.settings.voice_barge_in,
        }

    async def update_audio_settings(self, value: AudioSettingsUpdate) -> dict[str, Any]:
        updates = {
            "microphone": value.microphone,
            "speaker": value.speaker,
            "tts_voice": value.voice,
            "tts_speaking_rate": value.speech_speed,
            "audio_volume": value.volume,
            "listening_mode": value.conversation_mode,
            "always_listening_enabled": value.always_listening,
            "voice_barge_in": value.allow_interruption,
        }
        for key, item in updates.items():
            setattr(self.settings, key, item)
        listening_value = self.listening.config().model_copy(update={
            "enabled": value.always_listening,
            "mode": value.conversation_mode,
            "microphone": value.microphone,
            "barge_in": value.allow_interruption,
        })
        await self.listening.update(listening_value)
        await asyncio.to_thread(save_runtime_settings, updates)
        switched = None
        if self._tts_switcher:
            switched = await self._tts_switcher(self.settings.tts_provider, value.voice, value.speech_speed)
        return {"settings": self.audio_settings(), "tts": switched}

    async def transition(self, state: ConversationState, **safe: Any) -> None:
        self.state = state
        await self.event_bus.publish(EventType.CONVERSATION_STATE_CHANGED, state=state.value, **safe)

    async def _observe_runtime_event(self, event: Event) -> None:
        if event.type != EventType.REALTIME_STATUS_CHANGED:
            return
        raw = str(event.payload.get("status") or "")
        try:
            self.state = ConversationState(raw)
        except ValueError:
            return

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.conversation_engine,
            "state": self.state.value,
            "last_error": self._last_error,
            "audio": self.audio_settings(),
            "stt": {
                "provider": self.stt.name,
                "model": getattr(self.stt, "model_name", None),
                "loaded": bool(getattr(self.stt, "loaded", False)),
            },
            "tts": {
                "primary": getattr(self.orchestrator.tts, "primary_name", self.orchestrator.tts.name),
                "fallback": getattr(self.orchestrator.tts, "fallback_name", None),
            },
            "ollama": self.warm_manager.status() if self.warm_manager else None,
            "performance": dict(self.telemetry.last_metrics),
        }
