from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from app.network_watch.models import NetworkEvent, NetworkSeverity


class NetworkHistory:
    def __init__(self, database_path: Path, retention_days: int = 30) -> None:
        self.database_path = database_path
        self.retention_days = retention_days

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS network_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    duration_seconds REAL,
                    metrics TEXT NOT NULL DEFAULT '{}',
                    diagnosis TEXT,
                    recovered_at TEXT,
                    simulated INTEGER NOT NULL DEFAULT 0
                )"""
            )
            columns = {row[1] for row in await (await db.execute("PRAGMA table_info(network_events)")).fetchall()}
            if "simulated" not in columns:
                await db.execute("ALTER TABLE network_events ADD COLUMN simulated INTEGER NOT NULL DEFAULT 0")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_network_events_timestamp ON network_events(timestamp)"
            )
            await db.commit()

    async def add(self, event: NetworkEvent) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """INSERT INTO network_events(
                    timestamp,type,severity,message,duration_seconds,metrics,diagnosis,recovered_at,simulated
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    event.timestamp.isoformat(),
                    event.type,
                    event.severity.value,
                    event.message,
                    event.duration_seconds,
                    json.dumps(event.metrics, ensure_ascii=False),
                    event.diagnosis,
                    event.recovered_at.isoformat() if event.recovered_at else None,
                    int(event.simulated),
                ),
            )
            await db.commit()

    async def recent(self, hours: int = 24, limit: int = 100) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, min(hours, 24 * 30)))).isoformat()
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM network_events WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?",
                    (cutoff, max(1, min(limit, 500))),
                )
            ).fetchall()
        return [
            {
                **dict(row),
                "metrics": json.loads(row["metrics"] or "{}"),
                "simulated": bool(row["simulated"]),
            }
            for row in rows
        ]

    async def cleanup(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).isoformat()
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("DELETE FROM network_events WHERE timestamp < ?", (cutoff,))
            await db.commit()

    async def summary(self, hours: int = 1) -> dict:
        events = await self.recent(hours=hours, limit=500)
        return {
            "hours": hours,
            "event_count": len(events),
            "critical": sum(item["severity"] == NetworkSeverity.CRITICAL.value for item in events),
            "warnings": sum(item["severity"] == NetworkSeverity.WARNING.value for item in events),
            "recoveries": sum(item["severity"] == NetworkSeverity.RECOVERY.value for item in events),
            "latest": events[:5],
        }
