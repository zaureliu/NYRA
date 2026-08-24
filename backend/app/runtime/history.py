"""Persistent history of runtime operations (metadata only, no stdout dumps)."""

from __future__ import annotations

import aiosqlite

from app.tools.redaction import redact_secrets


class RuntimeHistory:
    def __init__(self, database_path) -> None:  # type: ignore[no-untyped-def]
        self.database_path = database_path
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    service TEXT NOT NULL,
                    action TEXT NOT NULL,
                    origin TEXT NOT NULL DEFAULT 'operator',
                    previous_state TEXT,
                    new_state TEXT,
                    duration_ms REAL NOT NULL DEFAULT 0,
                    success INTEGER NOT NULL,
                    error_code TEXT,
                    agent_run_id TEXT,
                    approval_id TEXT
                )
                """
            )
            await db.commit()
        self._initialized = True

    async def add(
        self,
        *,
        service: str,
        action: str,
        previous_state: str,
        new_state: str,
        duration_ms: float,
        success: bool,
        error_code: str | None = None,
        origin: str = "operator",
        agent_run_id: str | None = None,
        approval_id: str | None = None,
        timestamp=None,  # type: ignore[no-untyped-def]
    ) -> None:
        from datetime import datetime, timezone

        moment = timestamp or datetime.now(timezone.utc)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                "INSERT INTO runtime_events (timestamp, service, action, origin, previous_state, new_state,"
                " duration_ms, success, error_code, agent_run_id, approval_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    moment.isoformat(), service, redact_secrets(action), redact_secrets(origin),
                    previous_state, new_state, round(duration_ms, 1), int(success), error_code,
                    agent_run_id, approval_id,
                ),
            )
            await db.commit()

    async def recent(self, limit: int = 50, service: str | None = None) -> list[dict]:
        query = "SELECT * FROM runtime_events"
        params: list = []
        if service:
            query += " WHERE service = ?"
            params.append(service)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]
