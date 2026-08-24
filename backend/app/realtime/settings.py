from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from app.core.paths import DATA_ROOT
from app.realtime.models import PrivacyConfig, RealtimeConfig, RealtimeSettingsUpdate
from app.speech.voice_processor import VoiceProcessorConfig


V4_SETTINGS_PATH = DATA_ROOT / "settings-v4.json"


class V4Settings(BaseModel):
    realtime: RealtimeConfig = Field(default_factory=RealtimeConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    voice_processor: VoiceProcessorConfig = Field(default_factory=VoiceProcessorConfig)


class V4SettingsManager:
    def __init__(self, path: Path = V4_SETTINGS_PATH) -> None:
        self.path = path
        self.value = self.load()

    def load(self) -> V4Settings:
        try:
            return V4Settings.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return V4Settings()

    def update_realtime(self, value: RealtimeSettingsUpdate) -> V4Settings:
        self.value = self.value.model_copy(update={"realtime": value.realtime, "privacy": value.privacy})
        self.save()
        return self.value

    def update_voice_processor(self, value: VoiceProcessorConfig) -> V4Settings:
        self.value = self.value.model_copy(update={"voice_processor": value})
        self.save()
        return self.value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(self.value.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
