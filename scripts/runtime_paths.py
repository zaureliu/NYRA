"""Shared writable paths for development scripts; never place runtime state in Git."""

from __future__ import annotations

import os
from pathlib import Path


def runtime_root() -> Path:
    override = os.environ.get("KAZUMI_DATA_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        return (Path(local) / "KAZUMI").resolve()
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return (base / "KAZUMI").resolve()


RUNTIME_ROOT = runtime_root()
DATA_ROOT = RUNTIME_ROOT / "data"
LOG_ROOT = RUNTIME_ROOT / "logs"
REPORT_ROOT = RUNTIME_ROOT / "reports"
TEMP_ROOT = RUNTIME_ROOT / "tmp"


def ensure_script_directories() -> None:
    for path in (DATA_ROOT, LOG_ROOT, REPORT_ROOT, TEMP_ROOT):
        path.mkdir(parents=True, exist_ok=True)
