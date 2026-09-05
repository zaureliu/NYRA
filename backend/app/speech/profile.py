from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.core.paths import IDENTITY_ROOT, RESOURCE_ROOT
from app.speech.emotion import EmotionPlan, VoiceEmotion
from app.speech.tts_identity import KAZUMI_VOICE_ID


class VoiceSynthesisOptions(BaseModel):
    provider: Literal["chatterbox", "chatterbox_multilingual_v3", "chatterbox_ptbr", "kokoro", "edge_tts"] | None = None
    voice: str = KAZUMI_VOICE_ID
    speaking_rate: float = Field(0.97, ge=0.7, le=1.3)
    temperature: float = Field(0.8, ge=0.05, le=5)
    exaggeration: float = Field(0.5, ge=0.25, le=2)
    cfg_weight: float = Field(0.45, ge=0.2, le=1)
    seed: int = Field(42, ge=0, le=2_147_483_647)
    sentence_pause_ms: int = Field(220, ge=0, le=2000)
    paragraph_pause_ms: int = Field(420, ge=0, le=4000)
    edge_rate: str = Field("-5%", pattern=r"^[+-](?:0|[1-9]|1[0-9]|2[0-5])%$")
    edge_pitch: str = Field("+0Hz", pattern=r"^[+-](?:0|[1-9]|1[0-9]|20)Hz$")
    edge_volume: str = Field("+0%", pattern=r"^[+-](?:0|[1-9]|[1-9][0-9]|100)%$")
    emotion: str = "neutral"
    emotion_intensity: float = Field(0.2, ge=0, le=0.65)
    style_instruction: str = Field("", max_length=500)

    def with_emotion(self, plan: EmotionPlan) -> "VoiceSynthesisOptions":
        return self.model_copy(update={
            "emotion": plan.emotion.value,
            "emotion_intensity": plan.intensity,
            "style_instruction": plan.style_instruction,
        })

    def for_state(self, state: str, profile: dict) -> "VoiceSynthesisOptions":
        try:
            acoustic_emotion = VoiceEmotion(state).value
        except ValueError:
            acoustic_emotion = VoiceEmotion.NEUTRAL.value
        try:
            planned_emotion = VoiceEmotion(self.emotion).value
        except ValueError:
            planned_emotion = VoiceEmotion.NEUTRAL.value
        # A provider sem condicionamento emocional receives acoustic_state=neutral,
        # but the planned emotion still owns cache/telemetry metadata. This prevents
        # a happy cache entry from being silently reused as concerned later.
        if planned_emotion == VoiceEmotion.NEUTRAL.value and acoustic_emotion != VoiceEmotion.NEUTRAL.value:
            planned_emotion = acoustic_emotion
        modifier = profile.get("emotion_modifiers", {}).get(acoustic_emotion, {})
        intensity = self.emotion_intensity if planned_emotion == acoustic_emotion else 0.35
        scale = min(1.5, max(0.0, intensity / 0.35))
        return self.model_copy(
            update={
                "emotion": planned_emotion,
                "speaking_rate": max(
                    0.7,
                    min(1.3, self.speaking_rate * (1 + (float(modifier.get("rate_multiplier", 1)) - 1) * scale)),
                ),
                "exaggeration": max(0.25, min(2, self.exaggeration + float(modifier.get("exaggeration_delta", 0)) * scale)),
                "cfg_weight": max(0.2, min(1, self.cfg_weight + float(modifier.get("cfg_delta", 0)) * scale)),
                "sentence_pause_ms": round(
                    self.sentence_pause_ms * (1 + (float(modifier.get("pause_multiplier", 1)) - 1) * scale)
                ),
                "paragraph_pause_ms": round(
                    self.paragraph_pause_ms * (1 + (float(modifier.get("pause_multiplier", 1)) - 1) * scale)
                ),
            }
        )

    def cache_key_data(self, *, engine: str, model: str | None, text: str) -> dict[str, object]:
        return {
            "engine": engine,
            "model": model,
            "voice": self.voice,
            "emotion": self.emotion,
            "intensity": round(self.emotion_intensity, 3),
            "style": self.style_instruction,
            "speed": round(self.speaking_rate, 3),
            "text": text,
        }


def load_voice_profile(path: Path | None = None) -> tuple[dict, VoiceSynthesisOptions]:
    profile_path = path or IDENTITY_ROOT / "voice_profile.json"
    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    from app.brand_compat import preferences
    raw = preferences(raw)
    # Installed releases keep operator choices in LocalAppData. Merge those
    # choices over the bundled schema so new V2 emotion definitions reach an
    # existing installation without overwriting its selected provider/voice.
    template_path = RESOURCE_ROOT / "identity" / "voice_profile.json"
    if path is None and template_path.resolve() != profile_path.resolve() and template_path.is_file():
        template = json.loads(template_path.read_text(encoding="utf-8"))
        template_modifiers = dict(template.get("emotion_modifiers") or {})
        template_modifiers.update(raw.get("emotion_modifiers") or {})
        template_selection = dict(template.get("selection") or {})
        template_selection.update(raw.get("selection") or {})
        legacy_profile = raw.get("profile_id") != template.get("profile_id")
        raw = {
            **template,
            **raw,
            "emotion_modifiers": template_modifiers,
            "selection": template_selection,
        }
        if legacy_profile:
            # The approved Ava identity replaces the legacy Dora/blended
            # defaults in installed profiles while preserving operator prosody.
            for key in ("profile_id", "model", "edge_pitch", "paragraph_pause_ms"):
                raw[key] = template[key]
            if raw.get("voice") in {"pf_dora", "kazumi_voice_v2"}:
                raw["voice"] = template["voice"]
                raw["provider"] = template["provider"]
            raw["selection"] = dict(template.get("selection") or {})
    supported = VoiceSynthesisOptions.model_fields.keys()
    return raw, VoiceSynthesisOptions.model_validate(
        {key: value for key, value in raw.items() if key in supported}
    )
