from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.core.paths import DATA_ROOT


RUNTIME_SETTINGS_PATH = DATA_ROOT / "settings-v33.json"
RETIRED_RUNTIME_KEYS = {
    "listening_barge_in",
    "llm_auto_warmup_enabled",
    "tts_expressiveness",
    "silence_duration_ms",
}


def load_runtime_settings(path: Path = RUNTIME_SETTINGS_PATH) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_runtime_settings(
    updates: dict[str, Any], path: Path = RUNTIME_SETTINGS_PATH
) -> dict[str, Any]:
    """Atomically merge non-secret runtime preferences.

    The file is ignored by Git.  It contains toggles and thresholds only; audio,
    transcripts, topology and credentials are deliberately excluded.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_runtime_settings(path)
    for key in RETIRED_RUNTIME_KEYS:
        current.pop(key, None)
    current.update(updates)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)
    return current
