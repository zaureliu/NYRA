from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.core.paths import IDENTITY_ROOT


class VoiceSynthesisOptions(BaseModel):
    provider: Literal["chatterbox", "chatterbox_multilingual_v3", "chatterbox_ptbr", "kokoro", "edge_tts"] | None = None
    voice: str = "pf_dora"
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

    def for_state(self, state: str, profile: dict) -> "VoiceSynthesisOptions":
        modifier = profile.get("emotion_modifiers", {}).get(state, {})
        return self.model_copy(
            update={
                "speaking_rate": max(
                    0.7,
                    min(1.3, self.speaking_rate * float(modifier.get("rate_multiplier", 1))),
                ),
                "exaggeration": max(0.25, min(2, self.exaggeration + float(modifier.get("exaggeration_delta", 0)))),
                "cfg_weight": max(0.2, min(1, self.cfg_weight + float(modifier.get("cfg_delta", 0)))),
                "sentence_pause_ms": round(
                    self.sentence_pause_ms * float(modifier.get("pause_multiplier", 1))
                ),
                "paragraph_pause_ms": round(
                    self.paragraph_pause_ms * float(modifier.get("pause_multiplier", 1))
                ),
            }
        )


def load_voice_profile(path: Path | None = None) -> tuple[dict, VoiceSynthesisOptions]:
    profile_path = path or IDENTITY_ROOT / "voice_profile.json"
    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    supported = VoiceSynthesisOptions.model_fields.keys()
    return raw, VoiceSynthesisOptions.model_validate(
        {key: value for key, value in raw.items() if key in supported}
    )
