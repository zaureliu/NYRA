from __future__ import annotations

from datetime import datetime
from pathlib import Path

import aiosqlite

from app.tools.remote_models import RemoteExecutionResult, RemoteHistoryRecord
from app.tools.shell_models import ShellRiskLevel


class RemoteShellHistory:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS remote_executions (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    agent_run_id TEXT,
                    host TEXT NOT NULL,
                    address TEXT NOT NULL,
                    command TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    exit_code INTEGER,
                    duration_ms REAL NOT NULL,
                    success INTEGER NOT NULL,
                    timed_out INTEGER NOT NULL,
                    approval_required INTEGER NOT NULL,
                    approval_granted INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT ''
                )"""
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_remote_executions_time ON remote_executions(timestamp)")
            await db.commit()

    async def add(self, result: RemoteExecutionResult, timestamp: datetime) -> None:
        if not result.execution_id:
            return
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """INSERT INTO remote_executions(
                    id,timestamp,agent_run_id,host,address,command,risk_level,exit_code,duration_ms,
                    success,timed_out,approval_required,approval_granted,reason
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result.execution_id, timestamp.isoformat(), result.agent_run_id, result.host,
                    result.address, result.command, result.risk_level.value, result.exit_code,
                    result.duration_ms, int(result.success), int(result.timed_out),
                    int(result.approval_required), int(result.approval_granted), result.reason,
                ),
            )
            await db.commit()

    async def recent(self, limit: int = 50) -> list[RemoteHistoryRecord]:
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM remote_executions ORDER BY timestamp DESC LIMIT ?",
                    (max(1, min(limit, 200)),),
                )
            ).fetchall()
        return [
            RemoteHistoryRecord(
                id=row["id"], timestamp=datetime.fromisoformat(row["timestamp"]),
                agent_run_id=row["agent_run_id"], host=row["host"], address=row["address"],
                command=row["command"], risk_level=ShellRiskLevel(row["risk_level"]),
                exit_code=row["exit_code"], duration_ms=row["duration_ms"], success=bool(row["success"]),
                timed_out=bool(row["timed_out"]), approval_required=bool(row["approval_required"]),
                approval_granted=bool(row["approval_granted"]), reason=row["reason"],
            )
            for row in rows
        ]
