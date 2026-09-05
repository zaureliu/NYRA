from __future__ import annotations

import re
from enum import StrEnum

from app.events import EventBus, EventType
from app.memory import MemoryRepository
from app.persona_runtime.models import KazumiEmotion
from app.persona_runtime.policy import EmotionSignal


class EmotionalState(StrEnum):
    NEUTRAL = "neutral"
    FRIENDLY = "friendly"
    CONFIDENT = "confident"
    POSITIVE = "positive"
    HAPPY = "happy"
    RELIEVED = "relieved"
    CURIOUS = "curious"
    FOCUSED = "focused"
    CONCERNED = "concerned"
    WARNING = "warning"
    SERIOUS = "serious"
    EMPATHETIC = "empathetic"
    AMUSED = "amused"
    APOLOGETIC = "apologetic"
    UNCERTAIN = "uncertain"
    CALM = "calm"
    # Kept only for persisted V1 presentation state compatibility. The V2
    # speech planner allowlist deliberately does not generate it.
    TIRED = "tired"
    SURPRISED = "surprised"


TRANSITIONS: dict[EmotionalState, set[EmotionalState]] = {
    state: set(EmotionalState) for state in EmotionalState
}


class StateMachine:
    """A small, persistent state machine; states affect presentation, never safety."""

    def __init__(self, memory: MemoryRepository, event_bus: EventBus,
                 persona_runtime=None) -> None:
        self.memory = memory
        self.event_bus = event_bus
        self.persona_runtime = persona_runtime

    async def current(self) -> EmotionalState:
        if self.persona_runtime is not None:
            current = await self.persona_runtime.current_emotion()
            return EmotionalState(current.primary.value)
        try:
            return EmotionalState(await self.memory.get_state())
        except ValueError:
            return EmotionalState.NEUTRAL

    async def infer_and_transition(self, text: str) -> EmotionalState:
        if self.persona_runtime is not None:
            current = await self.persona_runtime.observe_user_text(text)
            return EmotionalState(current.primary.value)
        normalized = text.casefold()
        target = EmotionalState.NEUTRAL
        if re.search(r"\b(erro|falhou|offline|indisponível|risco|ataque|alerta)\b", normalized):
            target = EmotionalState.CONCERNED
        elif re.search(r"\b(uau|inesperado|surpresa|como assim)\b", normalized):
            target = EmotionalState.SURPRISED
        elif re.search(r"\b(haha|kkk|engraçad[oa]|boa piada)\b", normalized):
            target = EmotionalState.AMUSED
        elif re.search(r"\b(obrigad[oa]|funcionou|ótimo|excelente)\b", normalized):
            target = EmotionalState.HAPPY
        elif re.search(r"\b(analise|diagnostique|investigue|compare|logs?)\b", normalized):
            target = EmotionalState.FOCUSED
        elif "?" in text or re.search(r"\b(por que|como|qual|descobrir)\b", normalized):
            target = EmotionalState.CURIOUS
        return await self.transition(target)

    async def transition(
        self,
        target: EmotionalState,
        *,
        intensity: float = .25,
        confidence: float = .7,
        reason: str = "presentation_transition",
        priority: int = 50,
    ) -> EmotionalState:
        if self.persona_runtime is not None:
            # Legacy presentation-only states are mapped to the supported
            # runtime vocabulary rather than creating a competing ontology.
            if target == EmotionalState.TIRED:
                mapped = KazumiEmotion.NEUTRAL
            else:
                mapped = KazumiEmotion(target.value)
            current = await self.persona_runtime.apply_signal(EmotionSignal(
                mapped, intensity, confidence, priority, reason,
            ))
            return EmotionalState(current.primary.value)
        previous = await self.current()
        if target not in TRANSITIONS[previous]:
            target = EmotionalState.NEUTRAL
        if target != previous:
            await self.memory.set_state(target.value)
            await self.event_bus.publish(
                EventType.STATE_CHANGED, previous=previous.value, current=target.value
            )
        return target
