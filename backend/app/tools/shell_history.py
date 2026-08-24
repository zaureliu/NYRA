from __future__ import annotations

from datetime import datetime
from pathlib import Path

import aiosqlite

from app.tools.shell_models import ShellExecutionResult, ShellHistoryRecord, ShellRiskLevel


class ShellHistory:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS shell_executions (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    command TEXT NOT NULL,
                    working_directory TEXT NOT NULL,
                    shell TEXT NOT NULL,
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
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_shell_executions_timestamp ON shell_executions(timestamp)"
            )
            await db.commit()

    async def add(self, result: ShellExecutionResult, timestamp: datetime) -> None:
        if not result.execution_id:
            return
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """INSERT INTO shell_executions(
                    id, timestamp, command, working_directory, shell, risk_level,
                    exit_code, duration_ms, success, timed_out, approval_required,
                    approval_granted, reason
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.execution_id,
                    timestamp.isoformat(),
                    result.command,
                    result.working_directory,
                    result.shell,
                    result.risk_level.value,
                    result.exit_code,
                    result.duration_ms,
                    int(result.success),
                    int(result.timed_out),
                    int(result.approval_required),
                    int(result.approval_granted),
                    result.reason,
                ),
            )
            await db.commit()

    async def recent(self, limit: int = 50) -> list[ShellHistoryRecord]:
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM shell_executions ORDER BY timestamp DESC LIMIT ?",
                    (max(1, min(limit, 200)),),
                )
            ).fetchall()
        return [
            ShellHistoryRecord(
                id=row["id"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                command=row["command"],
                working_directory=row["working_directory"],
                shell=row["shell"],
                risk_level=ShellRiskLevel(row["risk_level"]),
                exit_code=row["exit_code"],
                duration_ms=row["duration_ms"],
                success=bool(row["success"]),
                timed_out=bool(row["timed_out"]),
                approval_required=bool(row["approval_required"]),
                approval_granted=bool(row["approval_granted"]),
                reason=row["reason"],
            )
            for row in rows
        ]

