"""Local SQLite persistence for identity, relationship and contextual emotion."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from app.persona_runtime.models import EmotionalState, NyraIdentity, RelationshipState


class PersonaRuntimeStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    async def initialize(self, identity: NyraIdentity) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                """CREATE TABLE IF NOT EXISTS nyra_identity_v1 (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    identity_version INTEGER NOT NULL, document TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            await db.execute(
                """CREATE TABLE IF NOT EXISTS relationship_state_v1 (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    document TEXT NOT NULL, updated_at TEXT NOT NULL
                )"""
            )
            await db.execute(
                """CREATE TABLE IF NOT EXISTS emotional_state_v1 (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    primary_emotion TEXT NOT NULL, document TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            document = identity.model_dump(mode="json")
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "INSERT OR IGNORE INTO nyra_identity_v1(singleton,identity_version,document,updated_at) VALUES(1,?,?,?)",
                (identity.identity_version, json.dumps(document, ensure_ascii=False), now),
            )
            relationship = RelationshipState()
            await db.execute(
                "INSERT OR IGNORE INTO relationship_state_v1(singleton,document,updated_at) VALUES(1,?,?)",
                (relationship.model_dump_json(), relationship.updated_at.isoformat()),
            )
            emotion = EmotionalState()
            await db.execute(
                "INSERT OR IGNORE INTO emotional_state_v1(singleton,primary_emotion,document,updated_at) VALUES(1,?,?,?)",
                (emotion.primary.value, emotion.model_dump_json(), emotion.last_updated.isoformat()),
            )
            await db.commit()

    async def load_identity(self) -> NyraIdentity | None:
        value = await self._document("nyra_identity_v1")
        try:
            return NyraIdentity.model_validate_json(value) if value else None
        except ValueError:
            return None

    async def save_identity(self, identity: NyraIdentity) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                "UPDATE nyra_identity_v1 SET identity_version=?,document=?,updated_at=datetime('now') WHERE singleton=1",
                (identity.identity_version, identity.model_dump_json()),
            )
            await db.commit()

    async def load_relationship(self) -> RelationshipState:
        value = await self._document("relationship_state_v1")
        try:
            return RelationshipState.model_validate_json(value) if value else RelationshipState()
        except ValueError:
            return RelationshipState()

    async def save_relationship(self, relationship: RelationshipState) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                "UPDATE relationship_state_v1 SET document=?,updated_at=? WHERE singleton=1",
                (relationship.model_dump_json(), relationship.updated_at.isoformat()),
            )
            await db.commit()

    async def load_emotion(self) -> EmotionalState:
        value = await self._document("emotional_state_v1")
        try:
            return EmotionalState.model_validate_json(value) if value else EmotionalState()
        except ValueError:
            return EmotionalState()

    async def save_emotion(self, emotion: EmotionalState) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                "UPDATE emotional_state_v1 SET primary_emotion=?,document=?,updated_at=? WHERE singleton=1",
                (emotion.primary.value, emotion.model_dump_json(), emotion.last_updated.isoformat()),
            )
            await db.commit()

    async def _document(self, table: str) -> str | None:
        if table not in {"nyra_identity_v1", "relationship_state_v1", "emotional_state_v1"}:
            raise ValueError("unknown persona table")
        async with aiosqlite.connect(self.database_path) as db:
            row = await (await db.execute(f"SELECT document FROM {table} WHERE singleton=1")).fetchone()
        return str(row[0]) if row else None
