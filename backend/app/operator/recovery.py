"""Recovery Engine / self-healing (spec Parte H §158-§172).

Transaction model (§163): action + previous_state + rollback_action +
verification. Backups BEFORE relevant mutations (file §160, registry §161).
Auto-rollback only when safe and authorized (§165) and NEVER blind: user
modifications after the snapshot block the rollback (§167).

States (§168): RECOVERY_REQUIRED / RECOVERING / RECOVERED / RECOVERY_FAILED.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.core.paths import DATA_ROOT
from app.tools.redaction import redact_secrets

_BACKUP_ROOT = DATA_ROOT / "recovery-backups"


class RecoveryState(StrEnum):
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RECOVERING = "RECOVERING"
    RECOVERED = "RECOVERED"
    RECOVERY_FAILED = "RECOVERY_FAILED"


_PROTECTED_PATHS = re.compile(
    r"(?i)^([a-z]:\\?$|[a-z]:\\(windows|program files)|c:\\users?$)"
)


class RecoveryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _new_backup_dir(transaction_id: str) -> Path:
    directory = _BACKUP_ROOT / time.strftime("%Y%m%d-%H%M%S") / transaction_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class RecoveryEngine:
    def __init__(self, approvals=None, event_bus=None, *, database_path: Path | None = None) -> None:
        self.approvals = approvals
        self.event_bus = event_bus
        self.database_path = database_path or (DATA_ROOT / "nyra.db")
        self._transactions: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        _BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
        await self._initialize_store()

    # ------------------------------------------------------------------ file backup
    async def prepare_file_backup(self, path: str, *, action: str = "") -> dict:
        """§160: backup before editing an important config."""
        source = Path(path).expanduser()
        if not source.is_file():
            raise RecoveryError("PATH_NOT_FILE", f"'{path}' não é arquivo.")
        if _PROTECTED_PATHS.match(str(source.parent)):
            raise RecoveryError("PROTECTED_PATH", "Caminho protegido não participa de transações.")
        async with self._lock:
            transaction_id = f"tx_{os.urandom(6).hex()}"
            backup_dir = _new_backup_dir(transaction_id)
            backup_path = backup_dir / source.name
            await asyncio.to_thread(shutil.copy2, source, backup_path)
            record = {
                "transaction_id": transaction_id,
                "action": redact_secrets(action)[:200],
                "kind": "file",
                "target": str(source),
                "backup_path": str(backup_path),
                "previous_hash": _sha256_file(source),
                "previous_mtime": source.stat().st_mtime,
                "state": RecoveryState.RECOVERY_REQUIRED.value,
                "created_at": time.time(),
            }
            self._transactions[transaction_id] = record
            await self._save(record)
            return {"success": True,
                    **{key: record[key] for key in ("transaction_id", "target", "backup_path", "state", "action")}}

    # -------------------------------------------------------------- registry backup
    async def prepare_registry_backup(self, key_path: str, value_name: str) -> dict:
        """§161: keep the previous registry value before a set."""
        key_clean = str(key_path).strip().replace("/", "\\")
        if not re.match(r"^(HKLM|HKCU|HKCR|HKU|HKCC)\\", key_clean, flags=re.IGNORECASE):
            raise RecoveryError("INVALID_HIVE", "Use HKLM/HKCU/HKCR/HKU/HKCC.")
        args = ["reg.exe", "query", key_clean]
        args += (["/v", value_name] if value_name else ["/ve"])
        completed = await asyncio.to_thread(
            lambda: subprocess.run(  # noqa: S603
                args, capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        )
        previous_output = completed.stdout.decode("utf-8", errors="replace")[:8000]
        transaction_id = f"tx_{os.urandom(6).hex()}"
        backup_dir = _new_backup_dir(transaction_id)
        backup_path = backup_dir / "registry-query.txt"
        backup_path.write_text(previous_output, encoding="utf-8")
        record = {
            "transaction_id": transaction_id,
            "action": f"registry set {key_clean}\\{value_name}",
            "kind": "registry",
            "target": f"{key_clean}\\{value_name}",
            "backup_path": str(backup_path),
            "previous_hash": hashlib.sha256(previous_output.encode("utf-8")).hexdigest(),
            "previous_mtime": None,
            "state": RecoveryState.RECOVERY_REQUIRED.value,
            "created_at": time.time(),
        }
        async with self._lock:
            self._transactions[transaction_id] = record
        await self._save(record)
        return {"success": True,
                **{key: record[key] for key in ("transaction_id", "target", "backup_path", "state", "action")}}

    async def commit(self, transaction_id: str) -> dict:
        """Mutation succeeded and was verified: close the transaction."""
        record = await self._get(transaction_id)
        if not record:
            return {"success": False, "error_code": "TRANSACTION_NOT_FOUND"}
        record["state"] = "COMMITTED"
        record["finished_at"] = time.time()
        await self._save(record)
        return {"success": True, "transaction_id": transaction_id, "state": "COMMITTED"}

    # --------------------------------------------------------------------- rollback
    async def rollback(self, transaction_id: str, *, approval_id: str | None = None,
                       auto: bool = False) -> dict:
        """Restore previous state (§164). Auto mode requires prior authorization
        AND a clean snapshot check (§165/§167)."""
        record = await self._get(transaction_id)
        if not record:
            return {"success": False, "error_code": "TRANSACTION_NOT_FOUND"}
        if record.get("state") == "COMMITTED":
            return {"success": False, "error_code": "ALREADY_COMMITTED",
                    "message": "Transação confirmada; rollback exige nova transação."}
        if record.get("state") == RecoveryState.RECOVERED.value:
            return {"success": True, "transaction_id": transaction_id, "state": record["state"]}
        decision = self._require_approval(
            description=f"Rollback de '{record['action'][:120]}'",
            risk="ELEVATED", approval_id=approval_id,
            binding_digest=self._rollback_binding(record),
        )
        if decision is not None:
            if auto:
                decision["auto_rollback_blocked"] = True
            return decision
        record["state"] = RecoveryState.RECOVERING.value
        await self._save(record)
        try:
            if record["kind"] == "file":
                verified = await self._rollback_file(record)
            elif record["kind"] == "registry":
                verified = await self._rollback_registry(record)
            else:
                verified = False
        except (OSError, subprocess.TimeoutExpired):
            verified = False
        refused_blindly = (
            not verified and record["state"] == RecoveryState.RECOVERY_REQUIRED.value
        )
        if not refused_blindly:  # §167: recusa mantém RECOVERY_REQUIRED
            record["state"] = (RecoveryState.RECOVERED.value if verified
                               else RecoveryState.RECOVERY_FAILED.value)
        record["finished_at"] = time.time()
        await self._save(record)
        await self._emit(verified)
        return {
            "success": verified,
            "transaction_id": transaction_id,
            "state": record["state"],
            "verification_status": "VERIFIED" if verified else ("RECOVERY_REQUIRED" if refused_blindly else "RECOVERY_FAILED"),
        }

    async def _rollback_file(self, record: dict) -> bool:
        """§167: refuse to clobber user edits made AFTER the snapshot."""
        target = Path(record["target"])
        backup_path = Path(record["backup_path"])
        if not backup_path.is_file():
            return False
        if target.is_file():
            current_hash = _sha256_file(target)
            snapshot_hash = record.get("previous_hash")
            # If file changed since snapshot but NOT by us (mtime moved and no
            # pending marker), we still restore only when hash matches what our
            # action wrote; otherwise flag for manual recovery.
            record.setdefault("post_action_hash", None)
            expected_post = record.get("post_action_hash")
            if expected_post and current_hash != expected_post and current_hash != snapshot_hash:
                record["state"] = RecoveryState.RECOVERY_REQUIRED.value
                await self._save(record)
                return False  # blind rollback refused (§167)
            target.unlink(missing_ok=True)
        else:
            parent_ok = not _PROTECTED_PATHS.match(str(target.parent))
            if not parent_ok:
                return False
        shutil.copy2(backup_path, target)
        restored_hash = _sha256_file(target)
        snapshot_hash = record.get("previous_hash")
        return restored_hash == snapshot_hash

    async def _rollback_registry(self, record: dict) -> bool:
        """Parse `reg query` snapshot and re-apply the previous value."""
        backup_path = Path(record["backup_path"])
        if not backup_path.is_file():
            return False
        content = backup_path.read_text("utf-8", errors="replace")
        line_match = re.search(
            r"^\s+(.+?)\s+(REG_[A-Z_]+)\s+(0x[0-9a-fA-F]+|.*)$", content, flags=re.MULTILINE,
        )
        target_parts = record["target"].rsplit("\\", 1)
        key_path, value_name = target_parts[0], target_parts[1]
        if not line_match:
            # Value did not exist before: delete it to restore state.
            completed = await asyncio.to_thread(
                lambda: subprocess.run(  # noqa: S603
                    ["reg.exe", "delete", key_path, "/v", value_name, "/f"],
                    capture_output=True, timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            )
            return completed.returncode == 0
        reg_type, raw_value = line_match.group(2), line_match.group(3).strip()
        args = ["reg.exe", "add", key_path, "/v", value_name, "/t", reg_type, "/d", raw_value, "/f"]
        completed = await asyncio.to_thread(
            lambda: subprocess.run(  # noqa: S603
                args, capture_output=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        )
        verify = await asyncio.to_thread(
            lambda: subprocess.run(  # noqa: S603
                ["reg.exe", "query", key_path, "/v", value_name],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        )
        verify_text = verify.stdout.decode("utf-8", errors="replace")
        return completed.returncode == 0 and raw_value in verify_text

    def mark_written(self, transaction_id: str, post_hash: str) -> None:
        record = self._transactions.get(transaction_id)
        if record is not None:
            record["post_action_hash"] = post_hash

    # ------------------------------------------------------------------- status
    async def status(self) -> dict:
        rows = await self._query_recent()
        states = {"RECOVERY_REQUIRED": 0, "RECOVERING": 0, "RECOVERED": 0, "RECOVERY_FAILED": 0}
        for row in rows:
            if row.get("state") in states:
                states[row["state"]] += 1
        return {"success": True, "transactions_recent": rows[:20], "counts": states}

    # ------------------------------------------------------------------- store
    async def _initialize_store(self) -> None:
        def work() -> None:
            with sqlite3.connect(self.database_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS operator_recovery (
                        transaction_id TEXT PRIMARY KEY,
                        action TEXT, kind TEXT, target TEXT, backup_path TEXT,
                        previous_hash TEXT, previous_mtime REAL,
                        state TEXT, created_at REAL, finished_at REAL,
                        post_action_hash TEXT
                    )
                    """
                )
        await asyncio.to_thread(work)

    async def _save(self, record: dict) -> None:
        def work() -> None:
            with sqlite3.connect(self.database_path) as connection:
                connection.execute(
                    """
                    INSERT INTO operator_recovery (transaction_id, action, kind, target, backup_path,
                                                   previous_hash, previous_mtime, state,
                                                   created_at, finished_at, post_action_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(transaction_id) DO UPDATE SET
                        state=excluded.state, finished_at=excluded.finished_at,
                        post_action_hash=excluded.post_action_hash
                    """,
                    (
                        record.get("transaction_id"), record.get("action"), record.get("kind"),
                        record.get("target"), record.get("backup_path"), record.get("previous_hash"),
                        record.get("previous_mtime"), record.get("state"), record.get("created_at"),
                        record.get("finished_at"), record.get("post_action_hash"),
                    ),
                )
        await asyncio.to_thread(work)

    async def _get(self, transaction_id: str) -> dict | None:
        if transaction_id in self._transactions:
            return self._transactions[transaction_id]

        def work() -> dict | None:
            with sqlite3.connect(self.database_path) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT * FROM operator_recovery WHERE transaction_id = ?", (transaction_id,)
                ).fetchone()
                return dict(row) if row else None

        loaded = await asyncio.to_thread(work)
        if loaded:
            self._transactions[transaction_id] = loaded
        return loaded

    async def _query_recent(self) -> list[dict]:
        def work() -> list[dict]:
            with sqlite3.connect(self.database_path) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT * FROM operator_recovery ORDER BY created_at DESC LIMIT 50"
                ).fetchall()
                return [dict(row) for row in rows]

        return await asyncio.to_thread(work)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _rollback_binding(record: dict[str, Any]) -> str:
        material = {
            key: record.get(key)
            for key in (
                "transaction_id", "kind", "target", "backup_path", "action",
                "previous_hash", "post_action_hash",
            )
        }
        serialized = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _require_approval(self, *, description: str, risk: str,
                          approval_id: str | None, binding_digest: str = "") -> dict | None:
        from app.tools.shell_models import ShellRiskLevel

        if self.approvals is None:
            return {"success": False, "error_code": "APPROVAL_REQUIRED"}
        approval_command = f"{description} params_sha256={binding_digest or 'none'}"
        fingerprint = self.approvals.fingerprint(
            approval_command, "recovery", "", 60, target="local",
        )
        if not approval_id:
            record = self.approvals.request(
                command=approval_command, shell="recovery", working_directory="",
                timeout_seconds=60, risk_level=ShellRiskLevel(risk), target="local",
                fingerprint=fingerprint,
            )
            return {"success": False, "error_code": "APPROVAL_REQUIRED",
                    "approval_required": True, "approval_id": record.approval_id}
        granted, reason = self.approvals.consume(approval_id, fingerprint)
        if not granted:
            return {"success": False, "error_code": "APPROVAL_INVALID", "message": reason}
        return None

    async def _emit(self, recovered: bool) -> None:
        from app.events import EventType

        if self.event_bus is None:
            return
        try:
            event = EventType.RECOVERY_EXECUTED
        except ValueError:
            event = EventType.ERROR
        try:
            await self.event_bus.publish(event, recovered=recovered)
        except Exception:  # noqa: BLE001
            pass
