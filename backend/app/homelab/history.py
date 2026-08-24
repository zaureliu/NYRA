"""Operational history for homelab actions and state transitions.

Persists metadata only (spec §93): resource ids, states, verification flags
and run correlation. No secrets are ever stored here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


class HomelabHistory:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS homelab_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    integration TEXT NOT NULL,
                    action TEXT NOT NULL,
                    previous_state TEXT,
                    new_state TEXT,
                    success INTEGER NOT NULL DEFAULT 0,
                    effect_verified INTEGER,
                    error_code TEXT,
                    agent_run_id TEXT,
                    turn_id TEXT,
                    detail TEXT NOT NULL DEFAULT ''
                )"""
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_homelab_history_time ON homelab_history(timestamp)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_homelab_history_resource ON homelab_history(resource)")
            await db.commit()

    async def add_action(
        self,
        *,
        resource: str,
        integration: str,
        action: str,
        success: bool,
        effect_verified: bool | None,
        error_code: str | None = None,
        agent_run_id: str | None = None,
        turn_id: str | None = None,
        previous_state: str | None = None,
        new_state: str | None = None,
        detail: str = "",
    ) -> int:
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute(
                """INSERT INTO homelab_history(
                    timestamp,resource,integration,action,previous_state,new_state,
                    success,effect_verified,error_code,agent_run_id,turn_id,detail
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    resource[:120], integration[:40], action[:60],
                    (previous_state or "")[:40], (new_state or "")[:40],
                    int(success),
                    None if effect_verified is None else int(effect_verified),
                    (error_code or "")[:64], agent_run_id, turn_id, detail[:400],
                ),
            )
            await db.commit()
            return int(cursor.lastrowid or 0)

    async def recent(self, limit: int = 50) -> list[dict]:
        bounded = max(1, min(limit, 200))
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """SELECT id,timestamp,resource,integration,action,previous_state,new_state,
                              success,effect_verified,error_code,agent_run_id,turn_id,detail
                       FROM homelab_history ORDER BY id DESC LIMIT ?""",
                    (bounded,),
                )
            ).fetchall()
        return [
            {
                "id": row["id"],
                "timestamp": row["timestamp"],
                "resource": row["resource"],
                "integration": row["integration"],
                "action": row["action"],
                "previous_state": row["previous_state"] or None,
                "new_state": row["new_state"] or None,
                "success": bool(row["success"]),
                "effect_verified": None if row["effect_verified"] is None else bool(row["effect_verified"]),
                "error_code": row["error_code"] or None,
                "agent_run_id": row["agent_run_id"],
                "turn_id": row["turn_id"],
                "detail": row["detail"],
            }
            for row in rows
        ]
