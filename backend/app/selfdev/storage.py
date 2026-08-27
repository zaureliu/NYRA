from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")


class ResourceLockError(RuntimeError):
    pass


class ResourceLock:
    """Cross-process lock with owner metadata and stale-lock detection."""

    def __init__(self, path: Path, *, stale_after_seconds: int = 3600) -> None:
        self.path = path
        self.stale_after_seconds = stale_after_seconds
        self.owner = f"{os.getpid()}-{uuid4().hex}"

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self._is_stale():
            try:
                self.path.unlink()
            except OSError:
                pass
        payload = json.dumps({
            "owner": self.owner,
            "pid": os.getpid(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).encode("utf-8")
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise ResourceLockError(f"resource locked: {self.path.stem}") from error
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)

    def release(self) -> None:
        try:
            value = load_json(self.path, {})
            if value.get("owner") == self.owner:
                self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def _is_stale(self) -> bool:
        value = load_json(self.path, {})
        try:
            created = datetime.fromisoformat(str(value.get("created_at")))
        except ValueError:
            return True
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - created > timedelta(seconds=self.stale_after_seconds)

    @contextmanager
    def held(self) -> Iterator[None]:
        self.acquire()
        try:
            yield
        finally:
            self.release()


def contained_path(root: Path, relative: str | Path) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("path escapes allowed root")
    return candidate
