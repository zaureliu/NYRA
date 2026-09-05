from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from pydantic import BaseModel, Field

from app.character.context import ContextBuilder
from app.character.response_style import apply_response_style
from app.character.state import StateMachine
from app.events import EventBus, EventType
from app.llm import LLMProvider
from app.memory import MemoryRepository
from app.memory.models import MemoryCategory, MemoryCreate
from app.speech.tts import TTSProvider
from app.speech.emotion import EmotionPlanner
from app.speech.profile import load_voice_profile
from app.speech.prosody import ProsodyProcessor
from app.speech.queue import SpeechPriority, SpeechQueue


conversation_logger = logging.getLogger("nyra.conversation")
error_logger = logging.getLogger("nyra.errors")


class ChatResult(BaseModel):
    response_id: str | None = None
    turn_id: str | None = None
    pipeline_status: str = "TEXT_COMPLETE"
    response: str
    display_text: str
    speech_text: str
    state: str
    emotion_intensity: float = 0.0
    audio_url: str | None = None
    audio_urls: list[str] = Field(default_factory=list)
    tts_provider: str | None = None
    timing: dict[str, float] = Field(default_factory=dict)


class ChatOrchestrator:
    def __init__(
        self,
        llm: LLMProvider,
        memory: MemoryRepository,
        state_machine: StateMachine,
        event_bus: EventBus,
        tts: TTSProvider,
        speech_queue: SpeechQueue | None = None,
    ) -> None:
        self.llm = llm
        self.memory = memory
        self.state_machine = state_machine
        self.event_bus = event_bus
        self.tts = tts
        self.speech_queue = speech_queue or SpeechQueue()
        self.context = ContextBuilder(memory, getattr(state_machine, "persona_runtime", None))
        self.prosody = ProsodyProcessor()
        self.emotion_planner = EmotionPlanner()
        self.network_watch = None
        self.sentinel_watch = None
        self.tools = None
        self.shell = None
        self.emotional_presence = None

    async def converse(self, text: str, synthesize: bool = True) -> ChatResult:
        pipeline_started = time.perf_counter()
        clean_text = text.strip()
        await self.event_bus.publish(EventType.USER_TEXT_RECEIVED, text=clean_text)
        state = await self.state_machine.infer_and_transition(clean_text)
        await self.memory.add(
            MemoryCreate(
                category=MemoryCategory.SHORT_TERM,
                role="user",
                content=clean_text,
                importance=5,
            )
        )
        runtime_context = ""
        direct_response = None
        if self.sentinel_watch is not None:
            direct_response = await self.sentinel_watch.explicit_command(clean_text)
        if self.network_watch is not None and re.search(
            r"\b(rede|internet|conex[aã]o|lat[eê]ncia|jitter|pacotes?|gateway|dns)\b",
            clean_text,
            re.IGNORECASE,
        ):
            runtime_context = json.dumps(self.network_watch.status(), ensure_ascii=False)
        if self.sentinel_watch is not None and re.search(r"\bsentinel\b", clean_text, re.IGNORECASE):
            sentinel_context = await self.sentinel_watch.summary(24)
            runtime_context += "\nUTAMO_SENTINEL=" + json.dumps(sentinel_context, ensure_ascii=False)
        messages = await self.context.build(clean_text, state, runtime_context)
        await self.event_bus.publish(EventType.LLM_PROCESSING, state=state.value)
        llm_started = time.perf_counter()
        response = direct_response or apply_response_style(await self.llm.chat(messages))
        llm_ms = round((time.perf_counter() - llm_started) * 1000, 1)
        emotion_plan = self.emotion_planner.plan(
            clean_text,
            response,
            context={"technical": bool(runtime_context)},
        )
        state = await self.state_machine.transition(
            state.__class__(emotion_plan.emotion.value),
            intensity=emotion_plan.intensity,
            confidence=emotion_plan.confidence,
            reason=emotion_plan.reason,
        )
        persona_runtime = getattr(self.state_machine, "persona_runtime", None)
        canonical_emotion = await persona_runtime.current_emotion() if persona_runtime is not None else None
        canonical_intensity = canonical_emotion.intensity if canonical_emotion is not None else emotion_plan.intensity
        prepared = self.prosody.prepare(response, provider=self.tts.name)
        await self.memory.add(
            MemoryCreate(
                category=MemoryCategory.SHORT_TERM,
                role="assistant",
                content=response,
                importance=5,
            )
        )
        await self.memory.retain()
        await self.event_bus.publish(
            EventType.NYRA_RESPONSE,
            text=response,
            display_text=prepared.display_text,
            speech_text=prepared.speech_text,
            state=state.value,
            emotion_intensity=canonical_intensity,
            emotion_engine_supported=self.tts.capabilities().supports_emotion,
        )
        conversation_logger.info(
            "conversation_turn",
            extra={"user_length": len(clean_text), "response_length": len(response)},
        )

        audio_url = None
        provider = None
        tts_ms = 0.0
        if synthesize and await self.tts.health():
            try:
                voice_build = self.emotional_presence.build_voice_style(
                    context={"source": "chat"},
                ) if self.emotional_presence is not None else None
                voice_interface = voice_build.presentation if voice_build else (
                    self.state_machine.persona_runtime.voice_interface(
                        provider_supports_emotion=self.tts.capabilities().supports_emotion,
                    )
                    if getattr(self.state_machine, "persona_runtime", None) is not None else None
                )
                await self.event_bus.publish(
                    EventType.TTS_STARTED, state=state.value,
                    voice_emotion=voice_interface.model_dump(mode="json") if voice_interface else None,
                    voice_style=voice_build.presentation.model_dump(mode="json") if voice_build else None,
                )
                tts_started = time.perf_counter()
                if voice_build:
                    options = voice_build.options
                    acoustic_state = voice_build.presentation.acoustic_emotion
                else:
                    _profile, defaults = load_voice_profile()
                    options = defaults.with_emotion(emotion_plan)
                    acoustic_state = state.value if self.tts.capabilities().supports_emotion else "neutral"
                audio_path = await self.speech_queue.synthesize(
                    self.tts, prepared.speech_text, acoustic_state, SpeechPriority.USER,
                    options=options,
                )
                tts_ms = round((time.perf_counter() - tts_started) * 1000, 1)
                audio_url = f"/api/audio/{Path(audio_path).name}"
                provider = self.tts.name
                await self.event_bus.publish(
                    EventType.TTS_FINISHED, state=state.value, audio_url=audio_url
                )
            except Exception as exc:
                error_logger.exception("tts_failed", extra={"error_type": type(exc).__name__})
        total_ms = round((time.perf_counter() - pipeline_started) * 1000, 1)
        return ChatResult(
            response=response,
            display_text=prepared.display_text,
            speech_text=prepared.speech_text,
            state=state.value,
            emotion_intensity=canonical_intensity,
            audio_url=audio_url,
            tts_provider=provider,
            timing={"llm_ms": llm_ms, "tts_ms": tts_ms, "total_ms": total_ms},
        )
