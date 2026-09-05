from dataclasses import asdict, dataclass, field
from enum import StrEnum
import re


class Nonverbal(StrEnum):
    LAUGH = "laugh"
    LIGHT_LAUGH = "light_laugh"
    CHUCKLE = "chuckle"
    SIGH = "sigh"
    HESITATION = "hesitation"
    THINKING_PAUSE = "thinking_pause"
    SURPRISE = "surprise"
    BREATH = "breath"
    PAUSE = "pause"


@dataclass
class SpeechPlan:
    spoken_text: str
    emotion: str
    intensity: float
    acoustic_emotion: bool
    style_supported: bool
    nonverbal_supported: bool
    nonverbals: list[str] = field(default_factory=list)

    def metadata(self) -> dict:
        return asdict(self)


def plan_speech(text: str, *, emotion: str, intensity: float, capabilities, nonverbals=()) -> SpeechPlan:
    """Realize existing Persona/Emotion output; never choose personality or facts."""
    cues = [Nonverbal(value).value for value in nonverbals]
    # Unsupported performance markup must not be pronounced by a local provider.
    tags = "|".join(re.escape(v.value) for v in Nonverbal)
    spoken = re.sub(rf"\[(?:{tags})\]", "", text, flags=re.I).strip()
    return SpeechPlan(spoken, emotion, max(0.0, min(1.0, intensity)),
                      bool(getattr(capabilities, "supports_emotion", False)),
                      bool(getattr(capabilities, "supports_styles", False)),
                      bool(getattr(capabilities, "supports_nonverbal", False)), cues)
