"""One-way synchronization from Persona Runtime to every presentation sink."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from app.emotional_presence.models import (
    AvatarEmotionPresentation,
    EmotionalPresenceSettings,
    EmotionalPresenceSettingsUpdate,
    EmotionalPresentationSnapshot,
    EmotionTransition,
)
from app.emotional_presence.settings import load_settings, save_settings
from app.emotional_presence.voice import VoicePresentationAdapter, VoiceStyleBuild
from app.events import Event, EventBus, EventType
from app.persona_runtime.models import EmotionalState, NyraEmotion
from app.persona_runtime.policy import EmotionSignal
from app.speech.profile import load_voice_profile


_TRANSITIONS: dict[NyraEmotion, tuple[int, str, int, int]] = {
    NyraEmotion.NEUTRAL: (420, "ease-out", 300, 180),
    NyraEmotion.FRIENDLY: (480, "ease-out", 700, 250),
    NyraEmotion.FOCUSED: (360, "ease-in-out", 900, 250),
    NyraEmotion.CONFIDENT: (420, "ease-out", 900, 300),
    NyraEmotion.POSITIVE: (500, "ease-out", 800, 300),
    NyraEmotion.HAPPY: (620, "ease-out", 1000, 350),
    NyraEmotion.RELIEVED: (620, "ease-out", 900, 350),
    NyraEmotion.CONCERNED: (480, "ease-in-out", 1000, 300),
    NyraEmotion.WARNING: (260, "ease-out", 1200, 180),
    NyraEmotion.SERIOUS: (300, "ease-out", 1200, 220),
    NyraEmotion.EMPATHETIC: (560, "ease-out", 1000, 320),
    NyraEmotion.CURIOUS: (460, "ease-in-out", 700, 260),
    NyraEmotion.SURPRISED: (280, "ease-out", 500, 220),
    NyraEmotion.AMUSED: (500, "ease-out", 800, 300),
    NyraEmotion.APOLOGETIC: (460, "ease-in-out", 900, 280),
    NyraEmotion.UNCERTAIN: (440, "ease-in-out", 800, 280),
    NyraEmotion.CALM: (650, "ease-out", 900, 350),
}


class EmotionPresentationCoordinator:
    """Presentation fan-out; never classifies or independently changes emotion."""

    def __init__(
        self,
        event_bus: EventBus,
        persona_runtime: Any,
        *,
        provider_getter: Callable[[], Any],
        avatar: Any,
        vtube_studio: Any,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.event_bus = event_bus
        self.persona_runtime = persona_runtime
        self.provider_getter = provider_getter
        self.avatar = avatar
        self.vtube_studio = vtube_studio
        self.clock = clock
        self.settings = load_settings()
        self.voice_adapter = VoicePresentationAdapter()
        self.current: EmotionalPresentationSnapshot | None = None
        self._started = False
        self._lock = asyncio.Lock()
        self._durations_ms: list[float] = []
        self._sync_count = 0

    async def start(self) -> None:
        if self._started:
            return
        await self.event_bus.subscribe(self.handle_event)
        self._started = True
        state = await self.persona_runtime.current_emotion()
        await self.synchronize(state, reason="runtime_resync")

    async def stop(self) -> None:
        if self._started:
            await self.event_bus.unsubscribe(self.handle_event)
        self._started = False

    async def handle_event(self, event: Event) -> None:
        if event.type == EventType.NYRA_EMOTION_CHANGED:
            try:
                state = EmotionalState(
                    primary=NyraEmotion(str(event.payload.get("emotion") or "neutral")),
                    intensity=float(event.payload.get("intensity") or 0.0),
                    confidence=float(event.payload.get("confidence") or 1.0),
                    reason=str(event.payload.get("reason") or "persona_runtime")[:180],
                )
            except (TypeError, ValueError):
                return
            await self.synchronize(state, reason=state.reason)
        elif event.type in {EventType.SPEECH_CANCELLED, EventType.USER_INTERRUPTED, EventType.TTS_FINISHED}:
            # Operational cleanup only. Emotion and expression deliberately remain.
            if self.avatar.state.mouth_open != 0:
                await self.avatar.update(mouth_open=0)

    async def synchronize(self, state: EmotionalState, *, reason: str) -> EmotionalPresentationSnapshot:
        started = time.perf_counter()
        async with self._lock:
            previous = self.current.emotion if self.current else state.primary
            transition_ms, ease, hold, cooldown = _TRANSITIONS[state.primary]
            transition = EmotionTransition(
                previous=previous,
                emotion=state.primary,
                intensity=state.intensity,
                transition_ms=0 if self.current is None else transition_ms,
                ease=ease,
                minimum_hold_ms=hold,
                cooldown_ms=cooldown,
                reason=reason,
            )
            avatar_result: AvatarEmotionPresentation | None = None
            if self.settings.enabled and self.settings.avatar_expression:
                shared_state = await self.avatar.apply_emotion(state.primary.value, state.intensity, transition.model_dump(mode="json"))
                vts = await self.vtube_studio.apply_emotion(state.primary.value, state.intensity, transition.model_dump(mode="json"))
                avatar_result = AvatarEmotionPresentation(
                    emotion=state.primary,
                    intensity=state.intensity,
                    state_expression=str(shared_state.expression),
                    vts_kind=str(vts.get("kind") or "offline"),
                    vts_target=vts.get("target"),
                    vts_applied=bool(vts.get("applied")),
                    fallback=vts.get("fallback"),
                    model_id=vts.get("model_id"),
                )
            self.current = EmotionalPresentationSnapshot(
                emotion=state.primary,
                intensity=state.intensity,
                confidence=state.confidence,
                reason=reason,
                transition=transition,
                voice=self.current.voice if self.current else None,
                avatar=avatar_result,
            )
            self._sync_count += 1
        self._record_duration(started)
        await self.event_bus.publish(
            EventType.NYRA_EMOTIONAL_PRESENCE_SYNCED,
            emotion=state.primary.value,
            intensity=state.intensity,
            transition=transition.model_dump(mode="json"),
            avatar=avatar_result.model_dump(mode="json") if avatar_result else None,
            source="persona_runtime",
        )
        return self.current.model_copy(deep=True)

    def build_voice_style(
        self,
        *,
        emotion: NyraEmotion | str | None = None,
        intensity: float | None = None,
        context: dict[str, Any] | None = None,
    ) -> VoiceStyleBuild:
        current = self.current
        selected = emotion or (current.emotion if current else self.persona_runtime.emotion.primary)
        level = intensity if intensity is not None else (current.intensity if current else self.persona_runtime.emotion.intensity)
        build = self.voice_adapter.build_voice_style(selected, level, context, self.provider_getter())
        if not self.settings.enabled or not self.settings.voice_expression:
            _profile, neutral_options = load_voice_profile()
            presentation = build.presentation.model_copy(update={
                "acoustic_emotion": "neutral",
                "native_controls": [],
                "degraded": build.presentation.emotion != NyraEmotion.NEUTRAL,
                "degradation_reason": "voice_expression_disabled",
            })
            build = VoiceStyleBuild(
                presentation=presentation,
                options=neutral_options.model_copy(update={
                    "emotion": presentation.emotion.value,
                    "emotion_intensity": presentation.intensity,
                    "style_instruction": "",
                }),
            )
        if self.current is not None:
            self.current = self.current.model_copy(update={"voice": build.presentation})
        return build

    async def controlled_transition(self, emotion: NyraEmotion | str, intensity: float) -> EmotionalState:
        selected = NyraEmotion(str(getattr(emotion, "value", emotion)).casefold())
        return await self.persona_runtime.apply_signal(EmotionSignal(
            selected, intensity, 1.0, 100, "controlled_presentation_test", 900, 3600,
        ))

    async def update_settings(self, update: EmotionalPresenceSettingsUpdate) -> EmotionalPresenceSettings:
        values = {key: value for key, value in update.model_dump().items() if value is not None}
        self.settings = self.settings.model_copy(update=values)
        save_settings(self.settings)
        state = await self.persona_runtime.current_emotion()
        await self.synchronize(state, reason="presentation_settings_changed")
        return self.settings.model_copy(deep=True)

    async def status(self) -> dict[str, Any]:
        if self.current is None:
            state = await self.persona_runtime.current_emotion()
            await self.synchronize(state, reason="status_resync")
        provider = self.provider_getter()
        voice = self.build_voice_style()
        vts = self.vtube_studio.status()
        live_vts = dict(vts.get("last_emotion_presentation") or {})
        if self.current.avatar is not None and live_vts:
            avatar = self.current.avatar.model_copy(update={
                "vts_kind": str(live_vts.get("kind") or "offline"),
                "vts_target": live_vts.get("target"),
                "vts_applied": bool(live_vts.get("applied")),
                "fallback": live_vts.get("fallback"),
                "model_id": live_vts.get("model_id"),
            })
            self.current = self.current.model_copy(update={"avatar": avatar})
        durations = self._durations_ms
        return {
            **self.current.model_dump(mode="json"),
            "state": "READY" if self._started else "OFFLINE",
            "settings": self.settings.model_dump(mode="json"),
            "voice": voice.presentation.model_dump(mode="json"),
            "voice_provider": str(getattr(provider, "active_provider", None) or getattr(provider, "name", "unknown")),
            "vts": {
                "state": vts.get("state"), "model": vts.get("model"), "model_id": vts.get("model_id"),
                "hotkeys": vts.get("hotkeys", []), "expressions": vts.get("expressions", []),
                "emotion_capabilities": vts.get("emotion_capabilities", {}),
                "last_emotion_presentation": vts.get("last_emotion_presentation"),
            },
            "vts_character_visible": (
                vts.get("vts_presence", {}).get("state") == "VTS_ACTIVE"
                and vts.get("vts_presence", {}).get("alpha") == "VALID"
            ),
            "performance": {
                "average_sync_ms": round(sum(durations) / len(durations), 4) if durations else 0.0,
                "samples": len(durations), "sync_count": self._sync_count,
            },
        }

    def _record_duration(self, started: float) -> None:
        self._durations_ms = [*self._durations_ms[-499:], (time.perf_counter() - started) * 1000]
