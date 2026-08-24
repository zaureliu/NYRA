from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

import aiosqlite

from app.agent.models import AgentRun, AgentRunState, AgentRunStatus, AgentStep


class AgentRunStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    turn_id TEXT,
                    conversation_id TEXT,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reasoning_steps INTEGER NOT NULL,
                    tool_calls INTEGER NOT NULL,
                    host_targets_json TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    pending_approval_id TEXT,
                    final_summary TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT ''
                )"""
            )
            table_info = await db.execute("PRAGMA table_info(agent_runs)")
            columns = {row[1] for row in await table_info.fetchall()}
            if "turn_id" not in columns:
                await db.execute("ALTER TABLE agent_runs ADD COLUMN turn_id TEXT")
            if "conversation_id" not in columns:
                await db.execute("ALTER TABLE agent_runs ADD COLUMN conversation_id TEXT")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_updated ON agent_runs(updated_at)")
            await db.commit()

    async def save(self, run: AgentRun) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """INSERT INTO agent_runs(
                    id,goal,turn_id,conversation_id,started_at,updated_at,state,status,reasoning_steps,tool_calls,
                    host_targets_json,steps_json,pending_approval_id,final_summary,error
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    goal=excluded.goal,turn_id=excluded.turn_id,conversation_id=excluded.conversation_id,
                    updated_at=excluded.updated_at,state=excluded.state,
                    status=excluded.status,reasoning_steps=excluded.reasoning_steps,
                    tool_calls=excluded.tool_calls,host_targets_json=excluded.host_targets_json,
                    steps_json=excluded.steps_json,pending_approval_id=excluded.pending_approval_id,
                    final_summary=excluded.final_summary,error=excluded.error""",
                (
                    run.id, run.goal, run.turn_id, run.conversation_id,
                    run.started_at.isoformat(), run.updated_at.isoformat(),
                    run.state.value, run.status.value, run.reasoning_steps, run.tool_calls,
                    json.dumps(run.host_targets, ensure_ascii=False),
                    json.dumps([step.model_dump(mode="json") for step in run.steps], ensure_ascii=False),
                    run.pending_approval_id, run.final_summary, run.error,
                ),
            )
            await db.commit()

    async def get(self, run_id: str) -> AgentRun | None:
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,))).fetchone()
        return self._from_row(row) if row else None

    async def recent(self, limit: int = 30) -> list[AgentRun]:
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM agent_runs ORDER BY updated_at DESC LIMIT ?",
                    (max(1, min(limit, 100)),),
                )
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row) -> AgentRun:
        return AgentRun(
            id=row["id"], goal=row["goal"], turn_id=row["turn_id"], conversation_id=row["conversation_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]), state=AgentRunState(row["state"]),
            status=AgentRunStatus(row["status"]), reasoning_steps=row["reasoning_steps"],
            tool_calls=row["tool_calls"], host_targets=json.loads(row["host_targets_json"]),
            steps=[AgentStep.model_validate(item) for item in json.loads(row["steps_json"])],
            pending_approval_id=row["pending_approval_id"], final_summary=row["final_summary"],
            error=row["error"],
        )
