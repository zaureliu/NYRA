"""Runtime log tailing with redaction and bounded output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.tools.redaction import redact_secrets


def read_log_tail(path: Path, lines: int, max_chars: int) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {"lines": [], "truncated": False, "exists": False, "path": str(path)}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {"lines": [f"erro lendo log: {type(exc).__name__}"], "truncated": False, "exists": True, "path": str(path)}
    text = raw.decode("utf-8", errors="replace")
    all_lines = text.splitlines()
    selected = all_lines[-lines:] if lines > 0 else []
    truncated = len(all_lines) > len(selected)
    output: list[str] = []
    budget = max_chars
    for line in selected:
        safe = redact_secrets(line)[:2000]
        if budget - len(safe) < 0:
            truncated = True
            break
        output.append(safe)
        budget -= len(safe)
    return {"lines": output, "truncated": truncated, "exists": True, "total_lines": len(all_lines), "path": str(path)}
