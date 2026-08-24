from __future__ import annotations

import os
from pathlib import Path

from app.core.paths import DATA_ROOT


TOKEN_PATH = DATA_ROOT / "secrets" / "sentinel-bridge-token.txt"


class SentinelSecretStore:
    """Stores only the bridge token, outside runtime settings and Git."""

    def __init__(self, path: Path = TOKEN_PATH, fallback: str = "") -> None:
        self.path = path
        self.fallback = fallback.strip()

    def load(self) -> str:
        environment = str(os.environ.get("NYRA_SENTINEL_BRIDGE_TOKEN", "") or "").strip()
        if environment:
            return environment
        try:
            if self.path.is_file():
                return self.path.read_text(encoding="utf-8").strip()
            return self.fallback
        except OSError:
            return self.fallback

    def save(self, token: str) -> None:
        value = token.strip()
        if len(value) < 32:
            raise ValueError("O token da bridge deve possuir ao menos 32 caracteres")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(value + "\n", encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, self.path)

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def configured(self) -> bool:
        return bool(self.load())
