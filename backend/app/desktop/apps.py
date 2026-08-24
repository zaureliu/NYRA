"""Desktop apps registry: trusted definitions loaded from config, validated safely."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.desktop.models import DesktopAppSpec


@dataclass
class DesktopAppEntry:
    app_id: str
    spec: DesktopAppSpec | None = None
    error: str | None = None


@dataclass
class DesktopAppsRegistry:
    entries: list[DesktopAppEntry] = field(default_factory=list)
    source_path: str = ""

    def get(self, app_id: str) -> DesktopAppSpec | None:
        for entry in self.entries:
            if entry.app_id == app_id and entry.spec is not None:
                return entry.spec
        return None

    def error_for(self, app_id: str) -> str | None:
        for entry in self.entries:
            if entry.app_id == app_id:
                return entry.error
        return None

    def valid_specs(self) -> list[DesktopAppSpec]:
        return [entry.spec for entry in self.entries if entry.spec is not None]


def load_desktop_apps(path: Path) -> DesktopAppsRegistry:
    registry = DesktopAppsRegistry(source_path=str(path))
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raw = {}
    except (yaml.YAMLError, OSError):
        registry.entries.append(DesktopAppEntry(app_id="registry_file", error="REGISTRY_FILE_UNREADABLE"))
        return registry
    apps = raw.get("apps")
    if not isinstance(apps, list):
        apps = []
    seen: dict[str, int] = {}

    for position, item in enumerate(apps):
        if not isinstance(item, dict) or not str(item.get("id", "")).strip():
            registry.entries.append(DesktopAppEntry(app_id=f"<invalid:{position}>", error="MISSING_APP_ID"))
            continue
        app_id = str(item["id"])
        try:
            spec = DesktopAppSpec.model_validate(item)
        except Exception as exc:
            registry.entries.append(DesktopAppEntry(
                app_id=app_id,
                error=f"INVALID_CONFIGURATION: {type(exc).__name__}: {str(exc)[:200]}",
            ))
            continue
        validation_error = _validate_spec(spec)
        if validation_error is None and app_id in seen:
            validation_error = f"DUPLICATE_APP_ID (first defined at position {seen[app_id]})"
        seen.setdefault(app_id, position)
        registry.entries.append(DesktopAppEntry(
            app_id=app_id,
            spec=None if validation_error else spec,
            error=validation_error,
        ))
    return registry


def _validate_spec(spec: DesktopAppSpec) -> str | None:
    if not spec.executable.strip():
        return "INVALID_CONFIGURATION: executable ausente"
    bare = spec.executable.replace("/", "\\").rsplit("\\", 1)[-1]
    if any(ch in bare for ch in "\"|;&<>") or ".." in spec.executable:
        return "INVALID_CONFIGURATION: executable com caracteres não permitidos"
    if spec.window_title_contains == [] and not spec.process_names:
        # sem nenhum critério de janela/processo a verificação seria impossível
        return "INVALID_CONFIGURATION: informe process_names ou window_title_contains"
    return None
