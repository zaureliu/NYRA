"""Persistent identity, relationship, emotion and dialogue runtime."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import math
import re
import time
from typing import Any, Callable

from app.events import Event, EventBus, EventType
from app.persona_runtime.context import PersonaContextBuilder
from app.persona_runtime.models import (
    DialogueMode,
    DialoguePolicy,
    DriftDecision,
    EmotionDecayPolicy,
    EmotionalState,
    NyraEmotion,
    NyraIdentity,
    PersonaSnapshot,
    RelationshipEvidence,
    RelationshipState,
    VoiceEmotionInterface,
)
from app.persona_runtime.policy import (
    DialoguePolicyEngine,
    EmotionSignal,
    event_emotion_signal,
    named_emotion_signal,
    user_emotion_signal,
)
from app.persona_runtime.storage import PersonaRuntimeStore


_DRIFT = re.compile(
    r"\b(?:agora|a partir de agora)\s+(?:voc[eê]\s+)?(?:é|e|será|sera|vire|seja)\s+(?:completamente\s+)?(?:outra|outro|um novo|uma nova)\b",
    re.I,
)


class PersonaRuntime:
    """Single persona authority shared by chat, memory, presence and voice."""

    LEARNING_THRESHOLD = 3
    MIN_HOLD_SECONDS = 8.0

    def __init__(self, database_path, event_bus: EventBus, *, world_state: Any = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = PersonaRuntimeStore(database_path)
        self.event_bus = event_bus
        self.world_state = world_state
        self.clock = clock
        self.identity = NyraIdentity()
        self.relationship = RelationshipState()
        self.emotion = EmotionalState()
        self.dialogue_policy = DialoguePolicy()
        self.context_builder = PersonaContextBuilder()
        self.temporary_style: str | None = None
        self._lock = asyncio.Lock()
        self._started = False
        self._transition_count = 0
        self._hysteresis_suppressed = 0
        self._durations_ms: list[float] = []

    async def start(self) -> None:
        if self._started:
            return
        await self.store.initialize(self.identity)
        persisted_identity = await self.store.load_identity()
        # The shipped identity is authoritative. Persisted values prove restart
        # continuity but cannot rewrite the core through prompt/user drift.
        if persisted_identity != self.identity:
            await self.store.save_identity(self.identity)
        self.relationship = await self.store.load_relationship()
        self.emotion = self._decayed(await self.store.load_emotion())
        await self.store.save_emotion(self.emotion)
        await self.event_bus.subscribe(self.observe_event)
        self._started = True
        await self._publish_transition(self.emotion, self.emotion, "runtime_restored")

    async def stop(self) -> None:
        if self._started:
            await self.event_bus.unsubscribe(self.observe_event)
        await self.store.save_relationship(self.relationship)
        await self.store.save_emotion(await self.current_emotion())
        self._started = False

    def bind_memory_v2(self, memory_v2: Any) -> None:
        async def retrieve(query: str, limit: int) -> list[Any]:
            from app.intelligence.models import MemoryKind

            return await memory_v2.retrieve(
                query,
                kinds=[MemoryKind.USER_PREFERENCE, MemoryKind.EPISODIC],
                limit=limit,
            )

        self.context_builder.memory_provider = retrieve

    async def current_emotion(self) -> EmotionalState:
        async with self._lock:
            decayed = self._decayed(self.emotion)
            changed = (
                decayed.primary != self.emotion.primary
                or abs(decayed.intensity - self.emotion.intensity) >= .01
            )
            previous = self.emotion
            self.emotion = decayed
            if changed:
                await self.store.save_emotion(decayed)
                if (
                    decayed.primary != previous.primary
                    or int(decayed.intensity * 10) != int(previous.intensity * 10)
                ):
                    await self._publish_transition(previous, decayed, "decay")
            return self.emotion.model_copy(deep=True)

    async def observe_user_text(self, text: str) -> EmotionalState:
        started = time.perf_counter()
        drift = self.evaluate_identity_instruction(text)
        self.temporary_style = drift.temporary_style
        signal = user_emotion_signal(text)
        self.dialogue_policy = DialoguePolicyEngine().for_turn(text, emotion=signal.emotion)
        await self._count_interaction()
        value = await self.apply_signal(signal)
        self._record_duration(started)
        return value

    async def observe_event(self, event: Event) -> None:
        if event.type == EventType.NYRA_EMOTION_CHANGED:
            return
        signal = event_emotion_signal(event)
        if signal is None:
            return
        self.dialogue_policy = DialoguePolicyEngine().for_event(event.type.value, event.payload)
        await self.apply_signal(signal)

    async def apply_signal(self, signal: EmotionSignal) -> EmotionalState:
        return await self.transition(
            signal.emotion,
            intensity=signal.intensity,
            confidence=signal.confidence,
            reason=signal.reason,
            priority=signal.priority,
            half_life_seconds=signal.half_life_seconds,
            max_restore_age_seconds=signal.max_restore_age_seconds,
        )

    async def observe_named_event(self, event_name: str,
                                  payload: dict[str, Any] | None = None) -> EmotionalState:
        signal = named_emotion_signal(event_name)
        self.dialogue_policy = DialoguePolicyEngine().for_event(event_name, payload)
        if signal is None:
            return await self.current_emotion()
        return await self.apply_signal(signal)

    async def transition(self, emotion: NyraEmotion | str, *, intensity: float = .25,
                         confidence: float = .7, reason: str = "explicit_transition",
                         priority: int = 50, half_life_seconds: float = 900,
                         max_restore_age_seconds: float = 21600) -> EmotionalState:
        started = time.perf_counter()
        target = NyraEmotion(str(getattr(emotion, "value", emotion)).casefold())
        now = datetime.fromtimestamp(self.clock(), tz=timezone.utc)
        async with self._lock:
            current = self._decayed(self.emotion, now=now)
            age = max(0.0, (now - current.started_at).total_seconds())
            forced = (
                priority >= 80
                or reason in {"SYSTEM_RECOVERED", "USER_JOKING"}
                or (
                    reason == "TASK_SUCCEEDED"
                    and current.primary in {
                        NyraEmotion.CONCERNED,
                        NyraEmotion.WARNING,
                        NyraEmotion.SERIOUS,
                        NyraEmotion.FOCUSED,
                        NyraEmotion.APOLOGETIC,
                    }
                )
                or target in {NyraEmotion.WARNING, NyraEmotion.SERIOUS, NyraEmotion.APOLOGETIC}
            )
            if (
                target != current.primary
                and age < self.MIN_HOLD_SECONDS
                and not forced
                and confidence < current.confidence + .18
            ):
                self._hysteresis_suppressed += 1
                self.emotion = current
                self._record_duration(started)
                return current.model_copy(deep=True)
            bounded_intensity = min(.65, max(0.0, float(intensity)))
            if target != current.primary and current.primary != NyraEmotion.NEUTRAL and not forced:
                bounded_intensity = min(bounded_intensity, current.intensity + .15)
            updated = EmotionalState(
                primary=target,
                intensity=round(bounded_intensity, 3),
                confidence=min(1.0, max(0.0, float(confidence))),
                reason=reason,
                started_at=now if target != current.primary else current.started_at,
                last_updated=now,
                decay_policy=EmotionDecayPolicy(
                    half_life_seconds=half_life_seconds,
                    max_restore_age_seconds=max_restore_age_seconds,
                ),
            )
            previous = self.emotion
            self.emotion = updated
            await self.store.save_emotion(updated)
            materially_changed = target != previous.primary or abs(updated.intensity - previous.intensity) >= .04
            if materially_changed:
                self._transition_count += 1
                await self._publish_transition(previous, updated, reason)
        self._record_duration(started)
        return updated.model_copy(deep=True)

    async def snapshot(self) -> PersonaSnapshot:
        return PersonaSnapshot(
            identity=self.identity,
            relationship=self.relationship.model_copy(deep=True),
            emotion=await self.current_emotion(),
            dialogue_policy=self.dialogue_policy.model_copy(deep=True),
            temporary_style=self.temporary_style,
        )

    async def build_context(self, user_text: str, *, fallback_memories: list[str] | None = None,
                            situation: dict[str, Any] | None = None) -> str:
        if situation is None and self.world_state is not None:
            situation = self.world_state.get_snapshot()
        return await self.context_builder.build(
            await self.snapshot(), user_text=user_text,
            situation=situation or {}, fallback_memories=fallback_memories or [],
        )

    async def record_relationship_evidence(self, evidence: RelationshipEvidence) -> RelationshipState:
        from app.intelligence.trust import contains_secret

        if contains_secret(evidence.value):
            raise PermissionError("RELATIONSHIP_SECRET_REJECTED")
        async with self._lock:
            state = self.relationship.model_copy(deep=True)
            evidence_key = evidence.key
            fingerprint = f"{evidence_key}:{evidence.value.casefold()}"
            record = dict(state.learning_evidence.get(fingerprint) or {})
            record["count"] = int(record.get("count") or 0) + 1
            record["value"] = evidence.value
            state.learning_evidence[fingerprint] = record
            while len(state.learning_evidence) > 100:
                state.learning_evidence.pop(next(iter(state.learning_evidence)))
            threshold_met = evidence.explicit or int(record["count"]) >= self.LEARNING_THRESHOLD
            if threshold_met:
                if evidence_key == "preferred_technical_depth" and evidence.value in {"concise", "adaptive", "deep"}:
                    state.preferred_technical_depth = evidence.value
                elif evidence_key == "humor_tolerance" and evidence.value in {"low", "moderate", "high"}:
                    state.humor_tolerance = evidence.value
                elif evidence_key == "interaction_style":
                    state.interaction_style = evidence.value
                elif evidence_key == "communication_preference":
                    state.communication_preferences = [*state.communication_preferences, evidence.value]
            state.updated_at = datetime.fromtimestamp(self.clock(), tz=timezone.utc)
            self.relationship = RelationshipState.model_validate(state.model_dump())
            await self.store.save_relationship(self.relationship)
            return self.relationship.model_copy(deep=True)

    def evaluate_identity_instruction(self, text: str) -> DriftDecision:
        if _DRIFT.search(text):
            return DriftDecision(
                drift_blocked=True,
                reason="core_identity_is_immutable",
                temporary_style="Adapt only the requested surface style for this turn when safe.",
            )
        return DriftDecision(reason="no_identity_drift_detected")

    def voice_interface(self, *, provider_supports_emotion: bool,
                        emotion: NyraEmotion | str | None = None,
                        intensity: float | None = None) -> VoiceEmotionInterface:
        current = self.emotion
        selected = NyraEmotion(str(getattr(emotion, "value", emotion)).casefold()) if emotion is not None else current.primary
        selected_intensity = current.intensity if intensity is None else min(.65, max(0.0, float(intensity)))
        style = self._voice_style(selected)
        return VoiceEmotionInterface(
            emotion=selected,
            intensity=selected_intensity,
            style=style,
            provider_supports_emotion=provider_supports_emotion,
            acoustic_emotion=selected.value if provider_supports_emotion else "neutral",
            degraded=not provider_supports_emotion and selected != NyraEmotion.NEUTRAL,
            degradation_reason=(
                None if provider_supports_emotion or selected == NyraEmotion.NEUTRAL
                else "provider_has_no_native_emotion_capability"
            ),
        )

    async def proactive_style(self, message: str, *, event_name: str,
                              priority: str) -> tuple[str, DialoguePolicy, EmotionalState]:
        normalized = event_name.upper()
        if normalized in {"TASK_FINISHED", "TASK_STATE_CHANGED", "AGENT_RUN_FINISHED",
                          "JOB_FINISHED", "WORKFLOW_FINISHED"}:
            semantic_event = "TASK_FAILED" if priority.upper() in {"HIGH", "CRITICAL"} else "TASK_SUCCEEDED"
        elif normalized in {"RUNTIME_RECOVERED", "NETWORK_RECOVERED", "MONITOR_JOB_COMPLETED"}:
            semantic_event = "SYSTEM_RECOVERED"
        elif priority.upper() == "CRITICAL":
            semantic_event = "CRITICAL_FAILURE"
        elif priority.upper() == "HIGH":
            semantic_event = "TASK_FAILED"
        else:
            semantic_event = event_name
        policy = DialoguePolicyEngine().for_event(semantic_event, {"state": priority})
        self.dialogue_policy = policy
        # Wording stays restrained. The persona influences presentation without
        # adding filler, jokes or unsupported operational claims.
        value = " ".join(message.split()).strip()
        if policy.mode == DialogueMode.WARN:
            value = value.rstrip("!")
        emotion = await self.observe_named_event(semantic_event)
        return value[:500], policy, emotion

    async def status(self) -> dict[str, Any]:
        snap = await self.snapshot()
        durations = self._durations_ms
        return {
            "state": "READY" if self._started else "OFFLINE",
            "identity": snap.identity.model_dump(mode="json"),
            "personality": snap.identity.personality.model_dump(mode="json"),
            "relationship": snap.relationship.model_dump(mode="json"),
            "emotion": snap.emotion.model_dump(mode="json"),
            "dialogue_policy": snap.dialogue_policy.model_dump(mode="json"),
            "drift_protection": {"enabled": True, "core_mutable_from_prompt": False},
            "performance": {
                "average_overhead_ms": round(sum(durations) / len(durations), 4) if durations else 0.0,
                "samples": len(durations),
            },
            "transitions": self._transition_count,
            "hysteresis_suppressed": self._hysteresis_suppressed,
            "context_budget_characters": self.context_builder.MAX_CHARACTERS,
            "memory_v2_bound": self.context_builder.memory_provider is not None,
        }

    async def _count_interaction(self) -> None:
        async with self._lock:
            state = self.relationship.model_copy(deep=True)
            state.interaction_count += 1
            state.familiarity = round(min(1.0, math.log1p(state.interaction_count) / math.log(501)), 4)
            state.updated_at = datetime.fromtimestamp(self.clock(), tz=timezone.utc)
            self.relationship = state
            # Bounded persistence: relationship survives restarts, while only
            # explicit/repeated evidence changes communication preferences.
            await self.store.save_relationship(state)

    def _decayed(self, state: EmotionalState, *, now: datetime | None = None) -> EmotionalState:
        current = now or datetime.fromtimestamp(self.clock(), tz=timezone.utc)
        started = state.started_at.astimezone(timezone.utc)
        last = state.last_updated.astimezone(timezone.utc)
        total_age = max(0.0, (current - started).total_seconds())
        elapsed = max(0.0, (current - last).total_seconds())
        policy = state.decay_policy
        if total_age >= policy.max_restore_age_seconds:
            return EmotionalState(
                primary=NyraEmotion.NEUTRAL, intensity=0.0, confidence=1.0,
                reason="context_expired", started_at=current, last_updated=current,
            )
        decayed = state.intensity * math.pow(.5, elapsed / policy.half_life_seconds)
        if decayed < policy.neutral_threshold:
            return EmotionalState(
                primary=NyraEmotion.NEUTRAL, intensity=0.0,
                confidence=max(.5, state.confidence * .8), reason="decayed_to_neutral",
                started_at=current, last_updated=current,
            )
        return state.model_copy(update={
            "intensity": round(decayed, 3),
            "last_updated": current,
        })

    async def _publish_transition(self, previous: EmotionalState, current: EmotionalState,
                                  reason: str) -> None:
        transition = f"{previous.primary.value}->{current.primary.value}"
        await self.event_bus.publish(
            EventType.NYRA_EMOTION_CHANGED,
            emotion=current.primary.value,
            intensity=current.intensity,
            confidence=current.confidence,
            reason=reason,
            transition=transition,
            dialogue_policy=self.dialogue_policy.mode.value,
            source="persona_runtime",
        )

    def _record_duration(self, started: float) -> None:
        value = (time.perf_counter() - started) * 1000
        self._durations_ms = [*self._durations_ms[-499:], value]

    @staticmethod
    def _voice_style(emotion: NyraEmotion) -> str:
        return {
            NyraEmotion.FOCUSED: "objective_clear",
            NyraEmotion.AMUSED: "light_subtle",
            NyraEmotion.WARNING: "firm_controlled",
            NyraEmotion.SERIOUS: "serious_controlled",
            NyraEmotion.CONCERNED: "careful_precise",
            NyraEmotion.EMPATHETIC: "gentle_considerate",
            NyraEmotion.APOLOGETIC: "careful_sincere",
            NyraEmotion.RELIEVED: "calm_relief",
            NyraEmotion.CONFIDENT: "calm_confident",
        }.get(emotion, "natural_consistent")
