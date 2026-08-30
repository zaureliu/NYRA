from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from app.usb.models import UsbDeviceObservation, UsbDeviceRecord, UsbHistoryEvent, utc_now


def default_usb_storage_root() -> Path:
    override = os.environ.get("NYRA_USB_DATA_HOME")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "NYRA" / "usb-devices"


class UsbDeviceRegistry:
    """Operator-owned USB inventory and bounded event history in local SQLite."""

    HISTORY_LIMIT = 1000

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_usb_storage_root()
        self.database_path = self.root / "registry.db"
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS usb_devices (
                    device_id TEXT PRIMARY KEY,
                    identity_basis TEXT NOT NULL,
                    identity_confidence TEXT NOT NULL,
                    name TEXT NOT NULL,
                    friendly_name TEXT,
                    category TEXT,
                    manufacturer TEXT,
                    product TEXT,
                    vid TEXT,
                    pid TEXT,
                    serial TEXT,
                    device_instance_id TEXT,
                    container_id TEXT,
                    device_class TEXT,
                    class_guid TEXT,
                    parent_instance_id TEXT,
                    com_port TEXT,
                    drive_letter TEXT,
                    volume_label TEXT,
                    filesystem TEXT,
                    size_bytes INTEGER,
                    interface_name TEXT,
                    network_state TEXT,
                    status TEXT NOT NULL,
                    relevance TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    registered INTEGER NOT NULL DEFAULT 0,
                    trusted INTEGER NOT NULL DEFAULT 0,
                    note TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    last_connection TEXT NOT NULL,
                    last_disconnection TEXT,
                    present_at_startup INTEGER NOT NULL DEFAULT 0,
                    identity_changed INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_usb_devices_registered
                    ON usb_devices(registered, last_seen DESC);
                CREATE INDEX IF NOT EXISTS idx_usb_devices_status
                    ON usb_devices(status, relevance);
                CREATE TABLE IF NOT EXISTS usb_history (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    friendly_name TEXT,
                    vid TEXT,
                    pid TEXT,
                    com_port TEXT,
                    drive_letter TEXT,
                    known INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    description TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_usb_history_timestamp
                    ON usb_history(timestamp DESC);
                """
            )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    async def observe(
        self,
        observation: UsbDeviceObservation,
        *,
        new_connection: bool,
        present_at_startup: bool = False,
        identity_changed: bool = False,
    ) -> tuple[UsbDeviceRecord, UsbDeviceRecord | None]:
        now = utc_now()
        async with self._lock:
            return await asyncio.to_thread(
                self._observe_sync, observation, now, new_connection,
                present_at_startup, identity_changed,
            )

    def _observe_sync(
        self,
        observation: UsbDeviceObservation,
        now: str,
        new_connection: bool,
        present_at_startup: bool,
        identity_changed: bool,
    ) -> tuple[UsbDeviceRecord, UsbDeviceRecord | None]:
        data = observation.model_dump(mode="json")
        with self._connection() as connection:
            previous_row = connection.execute(
                "SELECT * FROM usb_devices WHERE device_id = ?", (observation.device_id,)
            ).fetchone()
            previous = self._record(previous_row) if previous_row else None
            if previous is None:
                connection.execute(
                    """
                    INSERT INTO usb_devices (
                        device_id, identity_basis, identity_confidence, name,
                        friendly_name, category, manufacturer, product, vid, pid,
                        serial, device_instance_id, container_id, device_class,
                        class_guid, parent_instance_id, com_port, drive_letter,
                        volume_label, filesystem, size_bytes, interface_name,
                        network_state, status, relevance, metadata_json, registered,
                        trusted, note, first_seen, last_seen, last_connection,
                        last_disconnection, present_at_startup, identity_changed
                    ) VALUES (
                        :device_id, :identity_basis, :identity_confidence, :name,
                        NULL, :category, :manufacturer, :product, :vid, :pid,
                        :serial, :device_instance_id, :container_id, :device_class,
                        :class_guid, :parent_instance_id, :com_port, :drive_letter,
                        :volume_label, :filesystem, :size_bytes, :interface_name,
                        :network_state, 'CONNECTED', :relevance, :metadata_json, 0,
                        0, NULL, :first_seen, :last_seen, :last_connection,
                        NULL, :present_at_startup, :identity_changed
                    )
                    """,
                    {
                        **data,
                        "identity_confidence": observation.identity_confidence.value,
                        "relevance": observation.relevance.value,
                        "metadata_json": json.dumps(observation.metadata, ensure_ascii=False),
                        "first_seen": now,
                        "last_seen": now,
                        "last_connection": now,
                        "present_at_startup": int(present_at_startup),
                        "identity_changed": int(identity_changed),
                    },
                )
            else:
                connection.execute(
                    """
                    UPDATE usb_devices SET
                        identity_basis=:identity_basis,
                        identity_confidence=:identity_confidence,
                        name=:name,
                        category=CASE WHEN registered=1 AND category IS NOT NULL
                                      THEN category ELSE :category END,
                        manufacturer=:manufacturer, product=:product,
                        vid=:vid, pid=:pid, serial=:serial,
                        device_instance_id=:device_instance_id,
                        container_id=:container_id, device_class=:device_class,
                        class_guid=:class_guid, parent_instance_id=:parent_instance_id,
                        com_port=:com_port, drive_letter=:drive_letter,
                        volume_label=:volume_label, filesystem=:filesystem,
                        size_bytes=:size_bytes, interface_name=:interface_name,
                        network_state=:network_state, status='CONNECTED',
                        relevance=:relevance, metadata_json=:metadata_json,
                        last_seen=:last_seen,
                        last_connection=CASE WHEN :new_connection=1 THEN :last_seen
                                             ELSE last_connection END,
                        present_at_startup=:present_at_startup,
                        identity_changed=:identity_changed
                    WHERE device_id=:device_id
                    """,
                    {
                        **data,
                        "identity_confidence": observation.identity_confidence.value,
                        "relevance": observation.relevance.value,
                        "metadata_json": json.dumps(observation.metadata, ensure_ascii=False),
                        "last_seen": now,
                        "new_connection": int(new_connection),
                        "present_at_startup": int(present_at_startup),
                        "identity_changed": int(identity_changed),
                    },
                )
            row = connection.execute(
                "SELECT * FROM usb_devices WHERE device_id = ?", (observation.device_id,)
            ).fetchone()
            return self._record(row), previous

    async def mark_disconnected(self, device_id: str, *,
                                relevance: str | None = None) -> UsbDeviceRecord | None:
        now = utc_now()
        async with self._lock:
            def work() -> UsbDeviceRecord | None:
                with self._connection() as connection:
                    connection.execute(
                        "UPDATE usb_devices SET status='DISCONNECTED', "
                        "last_disconnection=?, last_seen=?, present_at_startup=0, "
                        "relevance=COALESCE(?, relevance) "
                        "WHERE device_id=?",
                        (now, now, relevance, device_id),
                    )
                    row = connection.execute(
                        "SELECT * FROM usb_devices WHERE device_id=?", (device_id,)
                    ).fetchone()
                    return self._record(row) if row else None
            return await asyncio.to_thread(work)

    async def update(self, device_id: str, values: dict[str, Any]) -> tuple[UsbDeviceRecord, bool]:
        allowed = {"friendly_name", "category", "trusted", "note", "registered"}
        updates = {key: values[key] for key in values if key in allowed}
        if not updates:
            record = await self.get(device_id)
            if record is None:
                raise KeyError(device_id)
            return record, False
        for key in ("friendly_name", "category", "note"):
            if key in updates:
                cleaned = str(updates[key] or "").strip()
                updates[key] = cleaned[:240] or None
        for key in ("trusted", "registered"):
            if key in updates:
                updates[key] = int(bool(updates[key]))
        async with self._lock:
            def work() -> tuple[UsbDeviceRecord, bool]:
                with self._connection() as connection:
                    before_row = connection.execute(
                        "SELECT * FROM usb_devices WHERE device_id=?", (device_id,)
                    ).fetchone()
                    if before_row is None:
                        raise KeyError(device_id)
                    before_registered = bool(before_row["registered"])
                    clauses = ", ".join(f"{key}=?" for key in updates)
                    connection.execute(
                        f"UPDATE usb_devices SET {clauses} WHERE device_id=?",  # noqa: S608 - keys allowlisted
                        (*updates.values(), device_id),
                    )
                    row = connection.execute(
                        "SELECT * FROM usb_devices WHERE device_id=?", (device_id,)
                    ).fetchone()
                    record = self._record(row)
                    return record, bool(record.registered and not before_registered)
            return await asyncio.to_thread(work)

    async def forget(self, device_id: str) -> UsbDeviceRecord:
        async with self._lock:
            def work() -> UsbDeviceRecord:
                with self._connection() as connection:
                    connection.execute(
                        "UPDATE usb_devices SET registered=0, trusted=0, friendly_name=NULL, "
                        "category=NULL, note=NULL WHERE device_id=?", (device_id,)
                    )
                    row = connection.execute(
                        "SELECT * FROM usb_devices WHERE device_id=?", (device_id,)
                    ).fetchone()
                    if row is None:
                        raise KeyError(device_id)
                    return self._record(row)
            return await asyncio.to_thread(work)

    async def get(self, device_id: str) -> UsbDeviceRecord | None:
        async with self._lock:
            def work() -> UsbDeviceRecord | None:
                with self._connection() as connection:
                    row = connection.execute(
                        "SELECT * FROM usb_devices WHERE device_id=?", (device_id,)
                    ).fetchone()
                    return self._record(row) if row else None
            return await asyncio.to_thread(work)

    async def find(self, query: str, *, connected_only: bool = False) -> list[UsbDeviceRecord]:
        needle = f"%{query.strip().casefold()}%"
        status_clause = "AND status='CONNECTED'" if connected_only else ""
        async with self._lock:
            def work() -> list[UsbDeviceRecord]:
                with self._connection() as connection:
                    rows = connection.execute(
                        f"""SELECT * FROM usb_devices
                            WHERE (lower(COALESCE(friendly_name,'')) LIKE ?
                               OR lower(name) LIKE ? OR lower(COALESCE(product,'')) LIKE ?
                               OR lower(COALESCE(com_port,'')) LIKE ?)
                            {status_clause}
                            ORDER BY status='CONNECTED' DESC, registered DESC, last_seen DESC
                            LIMIT 20""",  # noqa: S608 - status clause is fixed above
                        (needle, needle, needle, needle),
                    ).fetchall()
                    return [self._record(row) for row in rows]
            return await asyncio.to_thread(work)

    async def list_devices(self, *, registered: bool | None = None,
                           connected: bool | None = None,
                           include_internal: bool = False) -> list[UsbDeviceRecord]:
        where: list[str] = []
        params: list[Any] = []
        if registered is not None:
            where.append("registered=?")
            params.append(int(registered))
        if connected is not None:
            where.append("status=?")
            params.append("CONNECTED" if connected else "DISCONNECTED")
        if not include_internal:
            where.append("relevance='USER_RELEVANT'")
        clause = " WHERE " + " AND ".join(where) if where else ""
        async with self._lock:
            def work() -> list[UsbDeviceRecord]:
                with self._connection() as connection:
                    rows = connection.execute(
                        "SELECT * FROM usb_devices" + clause
                        + " ORDER BY status='CONNECTED' DESC, registered DESC, last_seen DESC",
                        params,
                    ).fetchall()
                    return [self._record(row) for row in rows]
            return await asyncio.to_thread(work)

    async def identity_changed_match(self, observation: UsbDeviceObservation) -> UsbDeviceRecord | None:
        if not observation.name or observation.identity_confidence.value == "LOW":
            return None
        known = await self.list_devices(registered=True, include_internal=True)
        tokens = {
            str(value or "").strip().casefold()
            for value in (observation.name, observation.product)
            if str(value or "").strip()
        }
        for record in known:
            if record.device_id == observation.device_id:
                continue
            known_tokens = {
                str(value or "").strip().casefold()
                for value in (record.friendly_name, record.name, record.product)
                if str(value or "").strip()
            }
            if tokens & known_tokens:
                return record
        return None

    async def append_history(self, event: UsbHistoryEvent) -> UsbHistoryEvent:
        async with self._lock:
            def work() -> UsbHistoryEvent:
                with self._connection() as connection:
                    cursor = connection.execute(
                        """INSERT INTO usb_history (
                            timestamp, event_type, device_id, name, friendly_name,
                            vid, pid, com_port, drive_letter, known, level, description
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            event.timestamp, event.event_type, event.device_id, event.name,
                            event.friendly_name, event.vid, event.pid, event.com_port,
                            event.drive_letter, int(event.known), event.level,
                            event.description[:1000],
                        ),
                    )
                    connection.execute(
                        "DELETE FROM usb_history WHERE event_id NOT IN "
                        "(SELECT event_id FROM usb_history ORDER BY event_id DESC LIMIT ?)",
                        (self.HISTORY_LIMIT,),
                    )
                    return event.model_copy(update={"event_id": int(cursor.lastrowid)})
            return await asyncio.to_thread(work)

    async def history(self, *, limit: int = 200,
                      event_type: str | None = None) -> list[UsbHistoryEvent]:
        limit = max(1, min(self.HISTORY_LIMIT, int(limit)))
        async with self._lock:
            def work() -> list[UsbHistoryEvent]:
                with self._connection() as connection:
                    if event_type:
                        rows = connection.execute(
                            "SELECT h.* FROM usb_history h JOIN usb_devices d "
                            "ON d.device_id=h.device_id WHERE h.event_type=? "
                            "AND d.relevance='USER_RELEVANT' "
                            "ORDER BY h.event_id DESC LIMIT ?", (event_type, limit),
                        ).fetchall()
                    else:
                        rows = connection.execute(
                            "SELECT h.* FROM usb_history h JOIN usb_devices d "
                            "ON d.device_id=h.device_id "
                            "WHERE d.relevance='USER_RELEVANT' "
                            "ORDER BY h.event_id DESC LIMIT ?", (limit,),
                        ).fetchall()
                    return [self._history_record(row) for row in rows]
            return await asyncio.to_thread(work)

    @staticmethod
    def _record(row: sqlite3.Row) -> UsbDeviceRecord:
        return UsbDeviceRecord(
            device_id=row["device_id"],
            identity_basis=row["identity_basis"],
            identity_confidence=row["identity_confidence"],
            name=row["name"], friendly_name=row["friendly_name"],
            category=row["category"], manufacturer=row["manufacturer"],
            product=row["product"], vid=row["vid"], pid=row["pid"],
            serial=row["serial"], device_instance_id=row["device_instance_id"],
            container_id=row["container_id"], device_class=row["device_class"],
            class_guid=row["class_guid"], parent_instance_id=row["parent_instance_id"],
            com_port=row["com_port"], drive_letter=row["drive_letter"],
            volume_label=row["volume_label"], filesystem=row["filesystem"],
            size_bytes=row["size_bytes"], interface_name=row["interface_name"],
            network_state=row["network_state"], status=row["status"],
            relevance=row["relevance"], metadata=json.loads(row["metadata_json"] or "{}"),
            registered=bool(row["registered"]), trusted=bool(row["trusted"]),
            note=row["note"], first_seen=row["first_seen"], last_seen=row["last_seen"],
            last_connection=row["last_connection"],
            last_disconnection=row["last_disconnection"],
            present_at_startup=bool(row["present_at_startup"]),
            identity_changed=bool(row["identity_changed"]),
        )

    @staticmethod
    def _history_record(row: sqlite3.Row) -> UsbHistoryEvent:
        return UsbHistoryEvent(
            event_id=row["event_id"], timestamp=row["timestamp"],
            event_type=row["event_type"], device_id=row["device_id"],
            name=row["name"], friendly_name=row["friendly_name"],
            vid=row["vid"], pid=row["pid"], com_port=row["com_port"],
            drive_letter=row["drive_letter"], known=bool(row["known"]),
            level=row["level"], description=row["description"],
        )
