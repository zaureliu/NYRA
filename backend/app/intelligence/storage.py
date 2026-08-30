from __future__ import annotations

from pathlib import Path

import aiosqlite


MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, (
        """CREATE TABLE IF NOT EXISTS intelligence_schema (
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS memory_v2 (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
            content_hash TEXT NOT NULL, source TEXT NOT NULL, category TEXT NOT NULL,
            project TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL,
            relevance REAL NOT NULL, sensitivity TEXT NOT NULL,
            expires_at TEXT, decay_half_life_days REAL, provenance TEXT NOT NULL,
            related_entities TEXT NOT NULL, metadata TEXT NOT NULL,
            conflict INTEGER NOT NULL DEFAULT 0, conflict_group TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(kind, project, content_hash)
        )""",
        """CREATE VIRTUAL TABLE IF NOT EXISTS memory_v2_fts USING fts5(
            memory_id UNINDEXED, content, tokenize='unicode61 remove_diacritics 2'
        )""",
        """CREATE TABLE IF NOT EXISTS knowledge_documents (
            id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
            size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, mime_type TEXT NOT NULL,
            metadata TEXT NOT NULL, indexed_at TEXT NOT NULL, chunk_count INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id TEXT PRIMARY KEY, document_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL, embedding TEXT NOT NULL, metadata TEXT NOT NULL,
            FOREIGN KEY(document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE,
            UNIQUE(document_id, chunk_index)
        )""",
        """CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
            chunk_id UNINDEXED, content, tokenize='unicode61 remove_diacritics 2'
        )""",
        """CREATE TABLE IF NOT EXISTS autonomous_tasks_v2 (
            task_id TEXT PRIMARY KEY, document TEXT NOT NULL, state TEXT NOT NULL,
            next_run TEXT, updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS intelligence_events (
            event_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, source TEXT NOT NULL,
            category TEXT NOT NULL, severity TEXT NOT NULL, entity TEXT,
            payload TEXT NOT NULL, correlation_id TEXT, evidence_level TEXT NOT NULL,
            confidence REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS execution_traces (
            trace_id TEXT NOT NULL, sequence INTEGER NOT NULL, timestamp TEXT NOT NULL,
            stage TEXT NOT NULL, component TEXT NOT NULL, operation TEXT NOT NULL,
            correlation_id TEXT, task_id TEXT, severity TEXT NOT NULL,
            duration_ms REAL, payload TEXT NOT NULL,
            PRIMARY KEY(trace_id, sequence)
        )""",
    )),
    (2, (
        "CREATE INDEX IF NOT EXISTS idx_memory_v2_kind_updated ON memory_v2(kind, updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_memory_v2_project ON memory_v2(project, updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_document ON knowledge_chunks(document_id, chunk_index)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_v2_state_next ON autonomous_tasks_v2(state, next_run)",
        "CREATE INDEX IF NOT EXISTS idx_events_correlation ON intelligence_events(correlation_id, timestamp DESC)",
        "CREATE INDEX IF NOT EXISTS idx_events_entity ON intelligence_events(entity, timestamp DESC)",
        "CREATE INDEX IF NOT EXISTS idx_traces_timestamp ON execution_traces(timestamp DESC)",
    )),
)


class IntelligenceStore:
    """Versioned logical domains sharing NYRA's existing local SQLite file."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    async def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute(MIGRATIONS[0][1][0])
            row = await (await db.execute("SELECT COALESCE(MAX(version), 0) FROM intelligence_schema")).fetchone()
            current = int(row[0] if row else 0)
            for version, statements in MIGRATIONS:
                if version <= current:
                    continue
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    for statement in statements:
                        await db.execute(statement)
                    await db.execute(
                        "INSERT INTO intelligence_schema(version, applied_at) VALUES(?, datetime('now'))",
                        (version,),
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

    async def health(self) -> dict:
        try:
            async with aiosqlite.connect(self.database_path) as db:
                check = await (await db.execute("PRAGMA quick_check")).fetchone()
                version = await (await db.execute("SELECT COALESCE(MAX(version), 0) FROM intelligence_schema")).fetchone()
            ok = bool(check and check[0] == "ok")
            return {"ok": ok, "state": "AVAILABLE" if ok else "DEGRADED",
                    "schema_version": int(version[0]), "quick_check": check[0] if check else "missing"}
        except Exception as error:
            return {"ok": False, "state": "OFFLINE", "schema_version": 0,
                    "error_code": type(error).__name__}

    async def counts(self) -> dict[str, int]:
        tables = {
            "memory": "memory_v2", "documents": "knowledge_documents",
            "chunks": "knowledge_chunks", "tasks": "autonomous_tasks_v2",
            "events": "intelligence_events", "traces": "execution_traces",
        }
        values: dict[str, int] = {}
        async with aiosqlite.connect(self.database_path) as db:
            for name, table in tables.items():
                row = await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
                values[name] = int(row[0] if row else 0)
        return values
