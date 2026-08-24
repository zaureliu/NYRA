from __future__ import annotations

import json
from pathlib import Path

from app.core.paths import DATA_ROOT, IDENTITY_ROOT
from .models import PronunciationDictionary, PronunciationRule


DEFAULT_PATH = IDENTITY_ROOT / "pronunciation_ptbr.defaults.json"
LEGACY_PATH = IDENTITY_ROOT / "pronunciation_ptbr.json"
OVERRIDE_PATH = DATA_ROOT / "pronunciation" / "user_overrides.json"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_dictionary() -> PronunciationDictionary:
    defaults = _load(DEFAULT_PATH)
    legacy = _load(LEGACY_PATH)
    overrides = _load(OVERRIDE_PATH)
    rules: dict[str, PronunciationRule] = {}
    for entry in defaults.get("rules", []):
        try:
            rule = PronunciationRule.model_validate(entry)
            rules[rule.canonical.casefold()] = rule
        except Exception:
            continue
    # Keep the V3 lexicon backwards compatible while migrating to rule entries.
    for canonical, spoken in legacy.get("terms", {}).items():
        key = canonical.casefold()
        rules.setdefault(key, PronunciationRule(canonical=canonical, spoken_form=spoken, aliases=[canonical]))
    for entry in overrides.get("rules", []):
        try:
            rule = PronunciationRule.model_validate(entry)
            rules[rule.canonical.casefold()] = rule
        except Exception:
            continue
    return PronunciationDictionary(version=int(defaults.get("version", 4)), rules=list(rules.values()))


def save_override(rule: PronunciationRule) -> None:
    current = _load(OVERRIDE_PATH)
    entries = current.get("rules", [])
    entries = [item for item in entries if str(item.get("canonical", "")).casefold() != rule.canonical.casefold()]
    entries.append(rule.model_dump(mode="json"))
    OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_PATH.write_text(json.dumps({"version": 1, "rules": entries}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def reset_override(canonical: str) -> None:
    current = _load(OVERRIDE_PATH)
    entries = [item for item in current.get("rules", []) if str(item.get("canonical", "")).casefold() != canonical.casefold()]
    OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_PATH.write_text(json.dumps({"version": 1, "rules": entries}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
