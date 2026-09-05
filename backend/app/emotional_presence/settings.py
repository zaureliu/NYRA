"""Local, atomic settings storage for emotional presentation switches."""

from __future__ import annotations

import os
from pathlib import Path

from app.core.paths import DATA_ROOT
from app.emotional_presence.models import EmotionalPresenceSettings


SETTINGS_PATH = DATA_ROOT / "emotional-presence-v1.json"


def load_settings(path: Path = SETTINGS_PATH) -> EmotionalPresenceSettings:
    try:
        return EmotionalPresenceSettings.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return EmotionalPresenceSettings()


def save_settings(value: EmotionalPresenceSettings, path: Path = SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(value.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
