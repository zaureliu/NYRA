from __future__ import annotations

from pathlib import Path
from typing import Any

from app.selfdev.models import NotificationType, SelfDevNotification
from app.selfdev.storage import atomic_write_json, load_json


class SelfDevNotificationCenter:
    def __init__(self, path: Path, *, max_items: int = 500) -> None:
        self.path = path
        self.max_items = max_items
        self._items: list[SelfDevNotification] = []
        self.load()

    def load(self) -> None:
        raw = load_json(self.path, {"notifications": []})
        values = raw.get("notifications", []) if isinstance(raw, dict) else []
        self._items = []
        for value in values:
            try:
                self._items.append(SelfDevNotification.model_validate(value))
            except (TypeError, ValueError):
                continue

    def persist(self) -> None:
        self._items = sorted(self._items, key=lambda item: item.created_at, reverse=True)[:self.max_items]
        atomic_write_json(self.path, {"notifications": [item.model_dump(mode="json") for item in self._items]})

    def add(self, kind: NotificationType, title: str, message: str, *, issue_id: str | None = None, details: dict[str, Any] | None = None) -> SelfDevNotification:
        item = SelfDevNotification(type=kind, title=title, message=message, issue_id=issue_id, details=details or {})
        self._items.insert(0, item)
        self.persist()
        return item

    def list(self, *, unread_only: bool = False, limit: int = 100) -> list[SelfDevNotification]:
        values = (item for item in self._items if not unread_only or not item.read)
        return list(values)[:max(1, min(limit, 500))]

    def unread_count(self) -> int:
        return sum(not item.read for item in self._items)

    def mark_read(self, notification_id: str) -> bool:
        item = next((value for value in self._items if value.notification_id == notification_id), None)
        if item is None:
            return False
        item.read = True
        self.persist()
        return True


class SelfDevDocumentation:
    def __init__(self, reports_root: Path) -> None:
        self.reports_root = reports_root

    def write_report(self, issue_id: str, payload: dict[str, Any]) -> Path:
        path = self.reports_root / f"{issue_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        sections = [f"# {issue_id}", ""]
        for title, value in payload.items():
            sections.extend([f"## {str(title).replace('_', ' ').title()}", "", self._render(value), ""])
        path.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _render(value: Any) -> str:
        if isinstance(value, list):
            return "\n".join(f"- {item}" for item in value) or "- N/A"
        if isinstance(value, dict):
            return "\n".join(f"- {key}: {item}" for key, item in value.items()) or "- N/A"
        return str(value)
