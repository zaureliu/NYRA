"""SQLite persistence for decisions, cooldowns and user-facing notices."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from typing import Iterable

import aiosqlite

from app.proactive_presence.models import (
    DecisionRecord,
    ProactiveCandidate,
    ProactiveNotification,
)


class ProactivePresenceStore:
    def __init__(self, intelligence_store, *, clock=time.time) -> None:
        self.database_path = intelligence_store.database_path
        self.clock = clock

    async def record_decision(self, record: DecisionRecord) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """INSERT INTO proactive_decisions_v1(
                    decision_id,event_id,event_type,source,entity,goal_id,priority,
                    score,decision,reason,repeat_count,created_at,document
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record.decision_id, record.event_id, record.event_type, record.source,
                 record.entity, record.goal_id, record.priority.value, record.score,
                 record.decision.value, record.reason, record.repeat_count,
                 record.created_at.isoformat(), record.model_dump_json()),
            )
            await db.commit()

    async def recent_decisions(self, limit: int = 100) -> list[DecisionRecord]:
        async with aiosqlite.connect(self.database_path) as db:
            rows = await (await db.execute(
                "SELECT document FROM proactive_decisions_v1 ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            )).fetchall()
        return [DecisionRecord.model_validate_json(row[0]) for row in rows]

    async def save_notification(self, notification: ProactiveNotification) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """INSERT INTO proactive_notifications_v1(
                    notification_id,decision_id,priority,read,created_at,document
                ) VALUES(?,?,?,?,?,?)""",
                (notification.notification_id, notification.decision_id,
                 notification.priority.value, int(notification.read),
                 notification.created_at.isoformat(), notification.model_dump_json()),
            )
            await db.commit()

    async def notifications(self, *, include_read: bool = True,
                            limit: int = 100) -> list[ProactiveNotification]:
        where = "" if include_read else "WHERE read=0"
        async with aiosqlite.connect(self.database_path) as db:
            rows = await (await db.execute(
                f"SELECT document FROM proactive_notifications_v1 {where} "
                "ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            )).fetchall()
        return [ProactiveNotification.model_validate_json(row[0]) for row in rows]

    async def mark_read(self, notification_id: str) -> bool:
        async with aiosqlite.connect(self.database_path) as db:
            row = await (await db.execute(
                "SELECT document FROM proactive_notifications_v1 WHERE notification_id=?",
                (notification_id,),
            )).fetchone()
            if not row:
                return False
            notification = ProactiveNotification.model_validate_json(row[0])
            notification.read = True
            await db.execute(
                "UPDATE proactive_notifications_v1 SET read=1,document=? WHERE notification_id=?",
                (notification.model_dump_json(), notification_id),
            )
            await db.commit()
        return True

    async def notifications_last_hour(self, now: datetime | None = None) -> int:
        current = now or datetime.fromtimestamp(self.clock(), timezone.utc)
        threshold = current - timedelta(hours=1)
        async with aiosqlite.connect(self.database_path) as db:
            row = await (await db.execute(
                "SELECT COUNT(*) FROM proactive_notifications_v1 WHERE created_at>=?",
                (threshold.isoformat(),),
            )).fetchone()
        return int(row[0] if row else 0)

    async def any_cooldown(self, keys: Iterable[str], now: float | None = None) -> bool:
        values = list(dict.fromkeys(keys))
        if not values:
            return False
        placeholders = ",".join("?" for _ in values)
        current = self.clock() if now is None else now
        async with aiosqlite.connect(self.database_path) as db:
            row = await (await db.execute(
                f"SELECT 1 FROM proactive_cooldowns_v1 "
                f"WHERE scope_key IN ({placeholders}) AND expires_at>? LIMIT 1",
                (*values, current),
            )).fetchone()
        return row is not None

    async def note_occurrence(self, dedup_key: str, now: float | None = None) -> int:
        current = self.clock() if now is None else now
        scope = f"semantic:{dedup_key}"
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """INSERT INTO proactive_cooldowns_v1(
                    scope_key,dedup_key,last_event_at,last_notified_at,expires_at,repeat_count
                ) VALUES(?,?,?,NULL,0,1)
                ON CONFLICT(scope_key) DO UPDATE SET
                    last_event_at=excluded.last_event_at,
                    repeat_count=proactive_cooldowns_v1.repeat_count+1""",
                (scope, dedup_key, current),
            )
            row = await (await db.execute(
                "SELECT repeat_count FROM proactive_cooldowns_v1 WHERE scope_key=?", (scope,),
            )).fetchone()
            await db.commit()
        return int(row[0] if row else 1)

    async def consume_cooldowns(self, dedup_key: str, scopes: dict[str, float],
                                now: float | None = None) -> None:
        current = self.clock() if now is None else now
        async with aiosqlite.connect(self.database_path) as db:
            for key, seconds in scopes.items():
                await db.execute(
                    """INSERT INTO proactive_cooldowns_v1(
                        scope_key,dedup_key,last_event_at,last_notified_at,expires_at,repeat_count
                    ) VALUES(?,?,?,?,?,1)
                    ON CONFLICT(scope_key) DO UPDATE SET
                        dedup_key=excluded.dedup_key,last_event_at=excluded.last_event_at,
                        last_notified_at=excluded.last_notified_at,
                        expires_at=MAX(proactive_cooldowns_v1.expires_at,excluded.expires_at)""",
                    (key, dedup_key, current, current, current + max(1, seconds)),
                )
            await db.commit()

    async def incident_open(self, incident_key: str) -> bool:
        async with aiosqlite.connect(self.database_path) as db:
            row = await (await db.execute(
                "SELECT is_open FROM proactive_incidents_v1 WHERE incident_key=?",
                (incident_key,),
            )).fetchone()
        return bool(row and row[0])

    async def set_incident(self, incident_key: str, *, is_open: bool,
                           notified: bool = False, now: float | None = None) -> None:
        current = self.clock() if now is None else now
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """INSERT INTO proactive_incidents_v1(incident_key,is_open,notified_at,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(incident_key) DO UPDATE SET
                is_open=excluded.is_open,
                notified_at=CASE WHEN excluded.notified_at IS NULL
                    THEN proactive_incidents_v1.notified_at ELSE excluded.notified_at END,
                updated_at=excluded.updated_at""",
                (incident_key, int(is_open), current if notified else None, current),
            )
            await db.commit()

    async def defer(self, candidate: ProactiveCandidate, *, ttl_seconds: int) -> None:
        expires = self.clock() + ttl_seconds
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """INSERT INTO proactive_deferred_v1(dedup_key,expires_at,created_at,document)
                VALUES(?,?,?,?) ON CONFLICT(dedup_key) DO UPDATE SET
                expires_at=excluded.expires_at,document=excluded.document""",
                (candidate.dedup_key, expires, now, candidate.model_dump_json()),
            )
            await db.commit()

    async def deferred(self) -> list[ProactiveCandidate]:
        current = self.clock()
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("DELETE FROM proactive_deferred_v1 WHERE expires_at<=?", (current,))
            rows = await (await db.execute(
                "SELECT document FROM proactive_deferred_v1 ORDER BY created_at LIMIT 100"
            )).fetchall()
            await db.commit()
        return [ProactiveCandidate.model_validate_json(row[0]) for row in rows]

    async def delete_deferred(self, dedup_key: str) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("DELETE FROM proactive_deferred_v1 WHERE dedup_key=?", (dedup_key,))
            await db.commit()

    async def counts(self) -> dict[str, int]:
        async with aiosqlite.connect(self.database_path) as db:
            decisions = await (await db.execute("SELECT COUNT(*) FROM proactive_decisions_v1")).fetchone()
            notifications = await (await db.execute("SELECT COUNT(*) FROM proactive_notifications_v1")).fetchone()
            unread = await (await db.execute("SELECT COUNT(*) FROM proactive_notifications_v1 WHERE read=0")).fetchone()
            deferred = await (await db.execute("SELECT COUNT(*) FROM proactive_deferred_v1")).fetchone()
        return {
            "decisions": int(decisions[0] if decisions else 0),
            "notifications": int(notifications[0] if notifications else 0),
            "unread": int(unread[0] if unread else 0),
            "deferred": int(deferred[0] if deferred else 0),
        }
