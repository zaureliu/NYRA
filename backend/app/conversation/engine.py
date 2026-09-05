from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from app.conversation.models import AudioSettingsUpdate, ConversationState, InterruptionTarget
from app.core.runtime_settings import save_runtime_settings
from app.core.turn import PipelineFailure, TurnContext, TurnError, new_turn_id
from app.events import Event, EventBus, EventType
from app.listening.models import ListeningMode
from app.natural_conversation.session import ConversationSession
from app.speech.tts_identity import KAZUMI_IDENTITY_ID, KAZUMI_VOICE_ID


TTSSwitcher = Callable[[str, str, float], Awaitable[dict[str, Any]]]
logger = logging.getLogger("kazumi.conversation.engine")


def _voice_stage_status(*, stt: str, decision: str, llm: str, tts: str, playback: str) -> dict[str, str]:
    """Stable, UI-safe stage summary for local voice turns."""
    return {
        "STT": stt,
        "DECISION": decision,
        "LLM": llm,
        "TTS": tts,
        "PLAYBACK": playback,
    }


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
        self.session = ConversationSession()
        self._voice_tasks: set[asyncio.Task] = set()
        if self.orchestrator is not None:
            self.orchestrator.voice_session = self.session
        self.last_speech_end: float | None = None

    async def start(self) -> None:
        self.orchestrator.emotion_planner.configure(
            emotion_mode=self.settings.voice_emotion_mode,
            expressiveness=self.settings.voice_expressiveness,
        )
        await self.event_bus.subscribe(self._observe_runtime_event)

    async def stop(self) -> None:
        await self.event_bus.unsubscribe(self._observe_runtime_event)
        for task in list(self._voice_tasks):
            task.cancel()
        if self._voice_tasks:
            await asyncio.gather(*self._voice_tasks, return_exceptions=True)
        self._voice_tasks.clear()
        self.session.close()

    def bind_tts_switcher(self, switcher: TTSSwitcher) -> None:
        self._tts_switcher = switcher

    async def speech_started(self, source: str = "microphone") -> bool:
        interrupted = False
        self.session.user_speaking = True
        self.session.last_activity_at = time.time()
        if (self.state == ConversationState.SPEAKING or self.listening.speaking or self.session.playing_response) and self.settings.voice_barge_in:
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

    async def _completed_transcript(self, transcription, speech_end):
        if not transcription.is_final:
            raise ValueError("Interim transcripts cannot become conversation turns")
        response_id = new_turn_id()
        self.telemetry.start(response_id, speech_end=speech_end or time.perf_counter())
        self.telemetry.mark(response_id, "t_stt_final")
        await self.event_bus.publish(EventType.STT_COMPLETED, response_id=response_id,
                                     turn_id=response_id, empty=not bool(transcription.text.strip()))
        return response_id, transcription

    async def direct_audio_turn(self, path, *, speech_end: float | None = None, transcription=None):
        if getattr(self.settings, "natural_conversation_enabled", False):
            if transcription is None:
                _, transcription = await self.transcribe(path, speech_end=speech_end)
            return await self._natural_turn(transcription, speech_end=speech_end)
        async with self._lock:
            response_id, transcription = (await self._completed_transcript(transcription, speech_end)
                                          if transcription is not None else await self.transcribe(path, speech_end=speech_end))
            if not transcription.text.strip():
                return {"accepted": False, "reason": "empty_transcription", "transcription": transcription.model_dump(mode="json")}
            turn = TurnContext(
                transcription.text,
                conversation_id="voice",
                approval_capable=False,
            )
            if turn.turn_id != response_id and response_id.startswith("turn_"):
                turn.turn_id = response_id
            await self.event_bus.publish(EventType.USER_SPEECH_FINAL, response_id=response_id,
                                         turn_id=turn.turn_id, text=transcription.text)
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

    async def listening_audio_turn(self, path, client_id: str, *, speech_end: float | None = None, transcription=None):
        if getattr(self.settings, "natural_conversation_enabled", False):
            allowed, reason = self.listening.can_process(client_id)
            if not allowed:
                return {"accepted": False, "reason": reason}
            if transcription is None:
                _, transcription = await self.transcribe(path, speech_end=speech_end)
            decision = self.listening.decide(transcription.text)
            if not decision.accepted:
                self.session.user_speaking = False
                return {"accepted": False, "reason": decision.reason, "decision": decision.model_dump(mode="json")}
            return await self._natural_turn(transcription, text=decision.text, speech_end=speech_end)
        async with self._lock:
            allowed, reason = self.listening.can_process(client_id)
            if not allowed:
                return {"accepted": False, "reason": reason, "status": self.listening.status()}
            self.listening.processing = True
            stage = "STT"
            response_id: str | None = None
            try:
                response_id, transcription = (await self._completed_transcript(transcription, speech_end)
                                              if transcription is not None else await self.transcribe(path, speech_end=speech_end))
                if not transcription.text.strip():
                    return {
                        "accepted": False,
                        "reason": "empty_transcription",
                        "transcription": transcription.model_dump(mode="json"),
                        "voice_stages": _voice_stage_status(
                            stt="COMPLETED", decision="SKIPPED", llm="SKIPPED",
                            tts="SKIPPED", playback="SKIPPED",
                        ),
                    }
                stage = "DECISION"
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
                        "voice_stages": _voice_stage_status(
                            stt="COMPLETED", decision="COMPLETED", llm="SKIPPED",
                            tts="SKIPPED", playback="SKIPPED",
                        ),
                    }
                turn = TurnContext(
                    decision.text,
                    conversation_id="always_listening",
                    approval_capable=False,
                )
                if turn.turn_id != response_id and response_id.startswith("turn_"):
                    turn.turn_id = response_id
                stage = "LLM"
                await self.event_bus.publish(EventType.USER_SPEECH_FINAL, response_id=response_id,
                                             turn_id=turn.turn_id, text=decision.text)
                chat = await self.orchestrator.converse(
                    decision.text,
                    synthesize=True,
                    speech_end=speech_end,
                    response_id=response_id,
                    turn=turn,
                )
                pipeline_status = str(getattr(chat, "pipeline_status", "TEXT_COMPLETE"))
                audio_urls = list(getattr(chat, "audio_urls", None) or [])
                audio_url = getattr(chat, "audio_url", None)
                has_audio = bool(audio_urls or audio_url)
                if pipeline_status == "AUDIO_DEGRADED":
                    tts_stage = "FAILED"
                elif has_audio:
                    tts_stage = "COMPLETED"
                else:
                    tts_stage = "UNAVAILABLE"
                return {
                    "accepted": True,
                    "reason": decision.reason,
                    "turn_id": turn.turn_id,
                    "transcription": transcription.model_dump(mode="json"),
                    "decision": decision.model_dump(mode="json"),
                    "chat": chat.model_dump(mode="json"),
                    # Playback is owned by the Desktop Presence and is only
                    # confirmed later through /listening/playback.
                    "voice_stages": _voice_stage_status(
                        stt="COMPLETED", decision="COMPLETED", llm="COMPLETED",
                        tts=tts_stage, playback="PENDING" if has_audio else "NOT_AVAILABLE",
                    ),
                }
            except Exception as exc:
                if isinstance(exc, PipelineFailure):
                    failure_stage = str(exc.error.stage or stage).upper()
                    exception_type = exc.error.exception_type
                    message = exc.error.message or str(exc)
                else:
                    failure_stage = stage
                    exception_type = type(exc).__name__
                    message = str(exc)
                absolute_path = str(Path(path).resolve()) if path is not None else "in-memory-stt-stream"
                logger.exception(
                    "always_listening_stage_failed stage=%s exception_type=%s message=%s path=%s",
                    failure_stage,
                    exception_type,
                    message,
                    absolute_path,
                )
                self._last_error = f"{failure_stage}:{exception_type}"
                await self.transition(
                    ConversationState.ERROR,
                    stage=failure_stage,
                    error=exception_type,
                )
                if isinstance(exc, PipelineFailure):
                    raise
                raise PipelineFailure(TurnError(
                    stage=failure_stage,
                    error_code=f"VOICE_{failure_stage}_FAILED",
                    exception_type=exception_type,
                    message=message,
                    recoverable=True,
                    turn_id=response_id,
                )) from exc
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
            "voice": self.orchestrator.tts.active_voice,
            "speech_speed": self.settings.tts_speaking_rate,
            "volume": self.settings.audio_volume,
            "conversation_mode": self.settings.listening_mode,
            "always_listening": self.listening.enabled,
            "allow_interruption": self.settings.voice_barge_in,
            "emotion_mode": self.settings.voice_emotion_mode,
            "expressiveness": self.settings.voice_expressiveness,
        }

    async def _natural_turn(self, transcription, *, text=None, speech_end=None):
        if not getattr(transcription, "is_final", True):
            raise ValueError("Interim transcripts cannot become conversation turns")
        text = (text if text is not None else transcription.text).strip()
        if not text:
            self.session.user_speaking = False
            return {"accepted": False, "reason": "empty_transcription"}
        if self.session.closed or len(self._voice_tasks) >= 4:
            return {"accepted": False, "reason": "session_backpressure"}
        turn = TurnContext(text, conversation_id=self.session.conversation_id, approval_capable=False)
        value = self.session.begin(turn, self.last_speech_end)
        self.last_speech_end = None
        await self.event_bus.publish(EventType.USER_SPEECH_FINAL, response_id=turn.response_id,
                                     turn_id=turn.turn_id, text=text, conversation_id=turn.conversation_id)

        async def respond():
            try:
                await self.orchestrator.converse(text, synthesize=True, speech_end=speech_end,
                                                 response_id=turn.response_id, turn=turn)
            except asyncio.CancelledError:
                self.session.interrupt(turn.response_id)
                raise
            except Exception as exc:
                self._last_error = f"VOICE:{type(exc).__name__}"
                logger.warning("voice_turn_failed turn_id=%s exception_type=%s", turn.turn_id, type(exc).__name__)
                await self.transition(ConversationState.LISTENING, reason="voice_turn_failed", turn_id=turn.turn_id)
            finally:
                value.ended_at = time.time()

        task = asyncio.create_task(respond(), name=f"kazumi-voice-{turn.turn_id}")
        self._voice_tasks.add(task)
        task.add_done_callback(self._voice_tasks.discard)
        # STT transport can now close. A slow model/tool/TTS never owns capture.
        return {"accepted": True, "deferred": True, "reason": "continuous_session",
                "turn_id": turn.turn_id, "conversation_id": turn.conversation_id,
                "transcription": transcription.model_dump(mode="json")}

    async def playback(self, payload) -> None:
        spoken = self.session.playback(payload)
        if spoken:
            from app.memory.models import MemoryCategory, MemoryCreate
            await self.orchestrator.memory.add(MemoryCreate(category=MemoryCategory.SHORT_TERM,
                                                            role="assistant", content=spoken, importance=5))
            await self.orchestrator.memory.retain()

    async def update_audio_settings(self, value: AudioSettingsUpdate) -> dict[str, Any]:
        updates = {
            "microphone": value.microphone,
            "speaker": value.speaker,
            "tts_voice": KAZUMI_VOICE_ID,
            "tts_voice_identity_version": "ava-v1",
            "tts_speaking_rate": value.speech_speed,
            "audio_volume": value.volume,
            "listening_mode": value.conversation_mode,
            "always_listening_enabled": value.always_listening,
            "voice_barge_in": value.allow_interruption,
            "voice_emotion_mode": value.emotion_mode,
            "voice_expressiveness": value.expressiveness,
        }
        for key, item in updates.items():
            setattr(self.settings, key, item)
        listening_value = self.listening.config().model_copy(update={
            "enabled": value.always_listening,
            "mode": ListeningMode(value.conversation_mode),
            "microphone": value.microphone,
            "barge_in": value.allow_interruption,
        })
        await self.listening.update(listening_value)
        await asyncio.to_thread(save_runtime_settings, updates)
        self.orchestrator.emotion_planner.configure(
            emotion_mode=value.emotion_mode,
            expressiveness=value.expressiveness,
        )
        switched = None
        if self._tts_switcher:
            switched = await self._tts_switcher(self.settings.tts_provider, KAZUMI_VOICE_ID, value.speech_speed)
        return {"settings": self.audio_settings(), "tts": switched}

    async def transition(self, state: ConversationState, **safe: Any) -> None:
        self.state = state
        await self.event_bus.publish(EventType.CONVERSATION_STATE_CHANGED, state=state.value, **safe)

    async def _observe_runtime_event(self, event: Event) -> None:
        if event.type == EventType.TASK_STATE_CHANGED:
            task_id = str(event.payload.get("task_id") or "")
            if self.session.find(event.payload.get("source_turn")):
                if event.payload.get("state") in {"COMPLETED", "FAILED", "CANCELLED"}:
                    self.session.pending_tool_runs.discard(task_id)
                elif len(self.session.pending_tool_runs) < 64:
                    self.session.pending_tool_runs.add(task_id)
        value = self.session.find(event.payload.get("response_id") or event.payload.get("turn_id"))
        if value:
            if event.type == EventType.LLM_TOKEN_RECEIVED:
                value.marks.setdefault("first_token", time.perf_counter())
                value.generated_text += str(event.payload.get("delta") or "")
            elif event.type == EventType.TTS_CHUNK_STARTED:
                value.marks.setdefault("tts_request", time.perf_counter())
                if event.payload.get("speech_text"):
                    value.chunks[int(event.payload.get("index", 0))] = str(event.payload["speech_text"])
            elif event.type == EventType.TTS_CHUNK_FINISHED:
                index = int(event.payload.get("index", 0))
                value.chunks[index] = str(event.payload.get("speech_text") or event.payload.get("display_text") or "")
                value.emotion = str(event.payload.get("state") or value.emotion)
            elif event.type == EventType.KAZUMI_RESPONSE:
                value.generated_text = str(event.payload.get("text") or value.generated_text)
            elif event.type == EventType.SPEECH_CANCELLED:
                self.session.interrupt(value.response_id)
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
            "natural_conversation": {**self.session.snapshot(), "enabled": getattr(self.settings, "natural_conversation_enabled", False),
                                     "active_background_tasks": len(self._voice_tasks),
                                     "speech_queue_state": getattr(getattr(self.orchestrator, "speech_queue", None), "pending", 0),
                                     "echo_guard": "browser_aec_and_playback_reference"},
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
                "primary_engine": self.orchestrator.tts.engine_id,
                "active_engine": self.orchestrator.tts.active_engine,
                "voice": self.orchestrator.tts.active_voice,
                "fallback_active": self.orchestrator.tts.fallback_active,
                "fallback_reason": self.orchestrator.tts.fallback_reason,
                "state": (
                    "FAILED" if getattr(self.orchestrator.tts, "name", "disabled") == "disabled"
                    else "DEGRADED" if bool(getattr(self.orchestrator.tts, "last_used_fallback", False))
                    else "READY"
                ),
                "identity": KAZUMI_IDENTITY_ID,
                "emotion_mode": self.settings.voice_emotion_mode,
                "expressiveness": self.settings.voice_expressiveness,
                "emotion_engine_supported": self.orchestrator.tts.capabilities().supports_emotion,
                "description": self.orchestrator.tts.describe(),
            },
            "ollama": self.warm_manager.status() if self.warm_manager else None,
            "performance": dict(self.telemetry.last_metrics),
        }
