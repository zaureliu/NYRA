from __future__ import annotations

from dataclasses import dataclass

from app.speech.profile import VoiceSynthesisOptions


@dataclass(frozen=True, slots=True)
class VoiceStylePlan:
    """Provider-agnostic description of how NYRA intends to sound.

    Persona/Emotion runtimes only produce this plan. Provider adapters own the
    conversion to their supported API parameters and may safely ignore fields.
    """

    emotion: str = "neutral"
    intensity: float = 0.0
    pace: str = "normal"
    energy: str = "controlled"
    instruction: str = ""

    @classmethod
    def from_options(cls, options: VoiceSynthesisOptions | None) -> "VoiceStylePlan":
        if options is None:
            return cls()
        pace = "slow" if options.speaking_rate < 0.92 else "fast" if options.speaking_rate > 1.08 else "normal"
        intensity = max(0.0, min(1.0, float(options.emotion_intensity)))
        energy = "expressive" if intensity >= 0.55 else "controlled" if intensity >= 0.2 else "subtle"
        return cls(
            emotion=str(options.emotion or "neutral")[:40],
            intensity=intensity,
            pace=pace,
            energy=energy,
            instruction=str(options.style_instruction or "")[:500],
        )

    def style_hash_data(self) -> dict[str, object]:
        return {
            "emotion": self.emotion,
            "intensity": round(self.intensity, 3),
            "pace": self.pace,
            "energy": self.energy,
            "instruction": self.instruction,
        }
