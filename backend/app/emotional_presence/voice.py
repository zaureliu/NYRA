"""Provider-agnostic voice style adapter driven only by EmotionalState."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.emotional_presence.models import VoiceEmotionSupport, VoiceStylePresentation
from app.persona_runtime.models import NyraEmotion
from app.speech.emotion import EmotionPlan
from app.speech.profile import VoiceSynthesisOptions, load_voice_profile


_DELIVERY: dict[NyraEmotion, str] = {
    NyraEmotion.NEUTRAL: "natural",
    NyraEmotion.FRIENDLY: "warm_relaxed",
    NyraEmotion.FOCUSED: "clear_precise",
    NyraEmotion.CONFIDENT: "calm_firm",
    NyraEmotion.POSITIVE: "subtly_positive",
    NyraEmotion.HAPPY: "restrained_positive_energy",
    NyraEmotion.RELIEVED: "relaxed_relief",
    NyraEmotion.CONCERNED: "careful",
    NyraEmotion.WARNING: "firm_slightly_slower",
    NyraEmotion.SERIOUS: "controlled",
    NyraEmotion.EMPATHETIC: "gentle_considerate",
    NyraEmotion.CURIOUS: "lightly_questioning",
    NyraEmotion.SURPRISED: "brief_increased_energy",
    NyraEmotion.AMUSED: "subtle_playful",
    NyraEmotion.APOLOGETIC: "careful_sincere",
    NyraEmotion.UNCERTAIN: "cautious",
    NyraEmotion.CALM: "relaxed_pacing",
}

_RATE_MULTIPLIERS: dict[NyraEmotion, float] = {
    NyraEmotion.FRIENDLY: 1.01,
    NyraEmotion.FOCUSED: .99,
    NyraEmotion.CONFIDENT: .99,
    NyraEmotion.POSITIVE: 1.01,
    NyraEmotion.HAPPY: 1.02,
    NyraEmotion.RELIEVED: .98,
    NyraEmotion.CONCERNED: .96,
    NyraEmotion.WARNING: .95,
    NyraEmotion.SERIOUS: .96,
    NyraEmotion.EMPATHETIC: .97,
    NyraEmotion.CURIOUS: 1.0,
    NyraEmotion.SURPRISED: 1.02,
    NyraEmotion.AMUSED: 1.01,
    NyraEmotion.APOLOGETIC: .96,
    NyraEmotion.UNCERTAIN: .97,
    NyraEmotion.CALM: .97,
}


@dataclass(frozen=True, slots=True)
class VoiceStyleBuild:
    presentation: VoiceStylePresentation
    options: VoiceSynthesisOptions


class VoicePresentationAdapter:
    """Maps one canonical emotion to controls a provider actually supports."""

    def build_voice_style(
        self,
        emotion: NyraEmotion | str,
        intensity: float,
        context: dict[str, Any] | None,
        provider: Any,
    ) -> VoiceStyleBuild:
        del context
        selected = NyraEmotion(str(getattr(emotion, "value", emotion)).casefold())
        level = round(min(.65, max(0.0, float(intensity))), 3)
        capabilities = provider.capabilities()
        capability_map = {
            "emotion": bool(getattr(capabilities, "supports_emotion", False)),
            "style": bool(getattr(capabilities, "supports_styles", False)),
            "style_instructions": bool(getattr(capabilities, "style_instructions", False)),
            "speed": bool(getattr(capabilities, "supports_native_speed", False)),
            "pitch": bool(getattr(capabilities, "supports_native_pitch", False)),
            "streaming": bool(getattr(capabilities, "supports_streaming", False)),
        }
        full = capability_map["emotion"]
        partial_controls = any(capability_map[key] for key in ("style", "style_instructions", "speed", "pitch"))
        support = VoiceEmotionSupport.FULL if full else VoiceEmotionSupport.PARTIAL if partial_controls else VoiceEmotionSupport.NONE
        _profile, defaults = load_voice_profile()
        plan = EmotionPlan.validated(selected.value, level, confidence=1.0, reason="persona_runtime")
        options = defaults.with_emotion(plan)
        native_controls: list[str] = []
        if capability_map["speed"] and selected != NyraEmotion.NEUTRAL:
            multiplier = _RATE_MULTIPLIERS.get(selected, 1.0)
            scaled = 1.0 + (multiplier - 1.0) * (level / .65 if level else 0.0)
            options = options.model_copy(update={
                "speaking_rate": min(1.3, max(.7, defaults.speaking_rate * scaled)),
            })
            native_controls.append("speed")
        if capability_map["style"] or capability_map["style_instructions"]:
            native_controls.append("style")
        if full:
            native_controls.append("emotion")
        provider_name = str(getattr(provider, "active_provider", None) or getattr(provider, "name", "unknown"))
        voice_identity = str(getattr(provider, "active_voice", None) or getattr(provider, "default_voice", "NYRA_VOICE"))
        degraded = selected != NyraEmotion.NEUTRAL and support != VoiceEmotionSupport.FULL
        presentation = VoiceStylePresentation(
            emotion=selected,
            intensity=level,
            provider=provider_name,
            voice_identity=voice_identity,
            delivery=_DELIVERY[selected],
            acoustic_emotion=selected.value if full else "neutral",
            emotion_support=support,
            native_controls=native_controls,
            speaking_rate=options.speaking_rate,
            pitch_adjustment_hz=0,
            style_instruction=plan.style_instruction if full or "style" in native_controls else "",
            capabilities=capability_map,
            degraded=degraded,
            degradation_reason=(
                None if not degraded else
                "provider_has_only_partial_native_controls" if support == VoiceEmotionSupport.PARTIAL
                else "provider_has_no_emotional_controls"
            ),
        )
        return VoiceStyleBuild(presentation=presentation, options=options)
