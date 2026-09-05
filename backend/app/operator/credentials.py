"""Credential Broker / Secret Vault (spec Parte D, §80-§99).

Store: Windows Credential Manager (CredReadW/CredWriteW/CredDeleteW, CRED_TYPE_
GENERIC) — audited per §82/§83. Fallback: DPAPI-protected file under data/
(crypt32 CryptProtectData) when Credential Manager is unavailable.

Hard rules enforced here:
    §84/§86  the LLM works with credential_id only; secrets never leave the
             broker except through resolve()/injection helpers used by internal
             backend callers (HTTP client, browser adapter, process env, SSH).
    §94      injection is scoped per request/process — never global env.
    §95-§99  every string crossing logs/tool results/API passes redaction and
             leak guards; API endpoints return metadata only.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.paths import DATA_ROOT
from app.tools.redaction import redact_secrets  # noqa: F401 - reexportado para consumidores do broker
from app.tools.shell_models import ShellRiskLevel

logger = logging.getLogger("kazumi.operator.credentials")

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_TARGET_PREFIX = "KAZUMI_CRED:"
_LEGACY_TARGET_PREFIX = "NYRA_CRED:"  # one-release WinCred migration compatibility
_VAULT_FILE = DATA_ROOT / "credentials-vault.bin"
_DPAPI_ENTROPY = b"NYRA::operator::credential-broker::v2"

_CREDENTIAL_ID_RE = r"^[a-z0-9_]{3,64}$"

_LEAK_TOKENS = ("secret_value", "raw_secret")


class CredentialError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


# --------------------------------------------------------------------- wincred
class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", ctypes.wintypes.FILETIME),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


def _wincred_available() -> bool:
    if os.name != "nt":
        return False
    try:
        advapi32 = ctypes.windll.advapi32
        return bool(advapi32.CredWriteW) and bool(advapi32.CredReadW)
    except (AttributeError, OSError):
        return False


def _cred_write(target: str, secret: bytes, comment: str, username: str) -> None:
    blob = (ctypes.c_byte * len(secret)).from_buffer_copy(secret)
    credential = _CREDENTIALW(
        Flags=0,
        Type=_CRED_TYPE_GENERIC,
        TargetName=target,
        Comment=comment[:256],
        LastWritten=ctypes.wintypes.FILETIME(0, 0),
        CredentialBlobSize=len(secret),
        CredentialBlob=ctypes.cast(blob, ctypes.POINTER(ctypes.c_byte)),
        Persist=_CRED_PERSIST_LOCAL_MACHINE,
        AttributeCount=0,
        Attributes=None,
        TargetAlias=None,
        UserName=username or "kazumi",
    )
    advapi32 = ctypes.windll.advapi32
    if not advapi32.CredWriteW(ctypes.byref(credential), 0):
        raise OSError(f"CredWriteW failed: {ctypes.GetLastError()}")


def _cred_read(target: str) -> bytes | None:
    advapi32 = ctypes.windll.advapi32
    credential_ptr = ctypes.c_void_p()
    if not advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(credential_ptr)):
        return None
    try:
        credential = ctypes.cast(credential_ptr, ctypes.POINTER(_CREDENTIALW)).contents
        size = int(credential.CredentialBlobSize)
        if not size:
            return b""
        buffer = ctypes.create_string_buffer(size)
        ctypes.memmove(buffer, credential.CredentialBlob, size)
        return buffer.raw
    finally:
        advapi32.CredFree(credential_ptr)


def _cred_delete(target: str) -> bool:
    advapi32 = ctypes.windll.advapi32
    return bool(advapi32.CredDeleteW(target, _CRED_TYPE_GENERIC, 0))


# ----------------------------------------------------------------------- dpapi
class _CRYPT_DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_protect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    entropy = ctypes.create_string_buffer(_DPAPI_ENTROPY, len(_DPAPI_ENTROPY))
    entropy_blob = _CRYPT_DATA_BLOB(len(_DPAPI_ENTROPY), ctypes.cast(entropy, ctypes.POINTER(ctypes.c_byte)))
    out = _CRYPT_DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(_CRYPT_DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_byte)))),
        "kazumi-credential",
        ctypes.byref(entropy_blob),
        None,
        None,
        0,
        ctypes.byref(out),
    )
    if not ok:
        raise OSError(f"CryptProtectData failed: {ctypes.GetLastError()}")
    try:
        buffer = ctypes.create_string_buffer(int(out.cbData))
        ctypes.memmove(buffer, out.pbData, int(out.cbData))
        return buffer.raw
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _dpapi_unprotect(blob: bytes) -> bytes | None:
    crypt32 = ctypes.windll.crypt32
    entropy = ctypes.create_string_buffer(_DPAPI_ENTROPY, len(_DPAPI_ENTROPY))
    entropy_blob = _CRYPT_DATA_BLOB(len(_DPAPI_ENTROPY), ctypes.cast(entropy, ctypes.POINTER(ctypes.c_byte)))
    in_blob = _CRYPT_DATA_BLOB(len(blob), ctypes.cast(ctypes.create_string_buffer(blob, len(blob)), ctypes.POINTER(ctypes.c_byte)))
    out = _CRYPT_DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, ctypes.byref(entropy_blob), None, None, 0, ctypes.byref(out),
    )
    if not ok:
        return None
    try:
        buffer = ctypes.create_string_buffer(int(out.cbData))
        ctypes.memmove(buffer, out.pbData, int(out.cbData))
        return buffer.raw
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


class CredentialVault:
    """Storage layer: Windows Credential Manager first, DPAPI file fallback."""

    def __init__(self) -> None:
        self.backend = "windows_credential_manager" if _wincred_available() else "dpapi_file"
        self._file_fallback_used = False

    def write(self, credential_id: str, secret: str, metadata: dict[str, Any]) -> None:
        payload = json.dumps({"secret": secret, "metadata": metadata}, ensure_ascii=False).encode("utf-8")
        target = f"{_TARGET_PREFIX}{credential_id}"
        if self.backend == "windows_credential_manager":
            try:
                _cred_write(target, payload, f"KAZUMI broker ({metadata.get('kind', 'generic')})", "kazumi")
                return
            except OSError as exc:
                logger.warning("wincred write failed, falling back to DPAPI file: %s", exc)
                self.backend = "dpapi_file"
                self._file_fallback_used = True
        self._write_dpapi(credential_id, payload)

    def read(self, credential_id: str) -> dict[str, Any] | None:
        raw: bytes | None = None
        if self.backend == "windows_credential_manager":
            raw = _cred_read(f"{_TARGET_PREFIX}{credential_id}")
            if raw is None:
                legacy = _cred_read(f"{_LEGACY_TARGET_PREFIX}{credential_id}")
                if legacy is not None:
                    # Copy inside the OS vault, verify bytes, retain legacy for
                    # rollback. No plaintext export and no credential logging.
                    _cred_write(f"{_TARGET_PREFIX}{credential_id}", legacy, "Kazumi migrated credential", "kazumi")
                    raw = _cred_read(f"{_TARGET_PREFIX}{credential_id}")
                    if raw != legacy:
                        raise CredentialError("MIGRATION_VERIFY_FAILED", "Credential migration verification failed")
        if raw is None and _VAULT_FILE.exists():
            raw = self._read_dpapi(credential_id)
        if raw is None:
            return None
        try:
            document = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(document, dict) or "secret" not in document:
            return None
        return document

    def delete(self, credential_id: str) -> bool:
        removed = False
        if self.backend == "windows_credential_manager":
            removed = _cred_delete(f"{_TARGET_PREFIX}{credential_id}")
            removed = _cred_delete(f"{_LEGACY_TARGET_PREFIX}{credential_id}") or removed
        if _VAULT_FILE.exists():
            entries = self._load_dpapi_store()
            if credential_id in entries:
                entries.pop(credential_id, None)
                self._save_dpapi_store(entries)
                removed = True
        return removed

    # ------------------------------------------------------------------- dpapi io
    def _write_dpapi(self, credential_id: str, payload: bytes) -> None:
        entries = self._load_dpapi_store()
        entries[credential_id] = payload.decode("utf-8")
        self._save_dpapi_store(entries)

    def _read_dpapi(self, credential_id: str) -> bytes | None:
        entries = self._load_dpapi_store()
        value = entries.get(credential_id)
        return value.encode("utf-8") if value else None

    def _load_dpapi_store(self) -> dict[str, str]:
        if not _VAULT_FILE.exists():
            return {}
        protected = _VAULT_FILE.read_bytes()
        plain = _dpapi_unprotect(protected)
        if plain is None:
            logger.error("DPAPI vault unreadable (different user/context?) — starting empty store")
            return {}
        try:
            return json.loads(plain.decode("utf-8"))
        except ValueError:
            return {}

    def _save_dpapi_store(self, entries: dict[str, str]) -> None:
        protected = _dpapi_protect(json.dumps(entries, ensure_ascii=False).encode("utf-8"))
        tmp = _VAULT_FILE.with_suffix(".tmp")
        tmp.write_bytes(protected)
        os.replace(tmp, _VAULT_FILE)


class CredentialBroker:
    """Metadata-safe façade over CredentialVault with single-use approvals."""

    def __init__(self, approvals=None) -> None:
        self.vault = CredentialVault()
        self.approvals = approvals
        self._index: dict[str, dict[str, Any]] = {}
        self._load_index()

    # ------------------------------------------------------------------ index api
    def _load_index(self) -> None:
        for credential_id in list(self._iter_known_ids()):
            document = self.vault.read(credential_id)
            if document:
                self._index[credential_id] = {
                    "credential_id": credential_id,
                    "kind": (document.get("metadata") or {}).get("kind", "generic"),
                    "description": (document.get("metadata") or {}).get("description", ""),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "has_secret": True,
                }

    def _iter_known_ids(self) -> set[str]:
        known: set[str] = set(self._index.keys())
        if self.vault.backend == "dpapi_file" and _VAULT_FILE.exists():
            try:
                known.update(self.vault._load_dpapi_store().keys())
            except Exception:  # noqa: BLE001
                pass
        return known

    # -------------------------------------------------------------------- public
    def create(self, credential_id: str, secret: str, *, kind: str = "generic",
               description: str = "", approval_id: str | None = None,
               operator_direct: bool = False) -> dict:
        import re as _re

        if not _re.match(_CREDENTIAL_ID_RE, credential_id):
            raise CredentialError("INVALID_CREDENTIAL_ID", "credential_id deve casar ^[a-z0-9_]{3,64}$.")
        if not secret or len(secret) > 4096:
            raise CredentialError("INVALID_SECRET", "Secret vazio ou acima de 4KB.")
        # §90: creation requires explicit operator interaction. The LLM-facing
        # tool surface never carries the secret; only the local API/UI does.
        if not operator_direct:
            decision = self._require_approval(
                description=f"Criar credencial '{credential_id}'", resource_key=f"credential:create:{credential_id}",
                risk="ELEVATED", approval_id=approval_id,
                binding_digest=self._binding_digest(secret, kind, description),
            )
            if decision is not None:
                return decision
        self.vault.write(credential_id, secret, {"kind": kind, "description": description})
        self._index[credential_id] = {
            "credential_id": credential_id, "kind": kind, "description": description,
            "updated_at": datetime.now(timezone.utc).isoformat(), "has_secret": True,
        }
        return {"success": True, "credential_id": credential_id, "backend": self.vault.backend}

    def delete(self, credential_id: str, approval_id: str | None = None,
               *, operator_direct: bool = False) -> dict:
        if not operator_direct:
            decision = self._require_approval(
                description=f"Excluir credencial '{credential_id}'",
                resource_key=f"credential:delete:{credential_id}", risk="DESTRUCTIVE", approval_id=approval_id,
            )
            if decision is not None:
                return decision
        existed = self.vault.delete(credential_id)
        self._index.pop(credential_id, None)
        return {"success": existed, "error_code": None if existed else "CREDENTIAL_NOT_FOUND"}

    def rotate(self, credential_id: str, new_secret: str, approval_id: str | None = None) -> dict:
        current = self.vault.read(credential_id)
        if not current:
            return {"success": False, "error_code": "CREDENTIAL_NOT_FOUND"}
        decision = self._require_approval(
            description=f"Rotacionar credencial '{credential_id}'",
            resource_key=f"credential:rotate:{credential_id}", risk="ELEVATED", approval_id=approval_id,
            binding_digest=self._binding_digest(new_secret, current.get("metadata") or {}),
        )
        if decision is not None:
            return decision
        metadata = current.get("metadata") or {}
        self.vault.write(credential_id, new_secret, metadata)
        self._index[credential_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        return {"success": True, "credential_id": credential_id}

    def list_credentials(self) -> dict:
        items = []
        for credential_id in sorted(self._index):
            entry = dict(self._index[credential_id])
            entry.pop("secret", None)
            items.append(entry)
        return {"success": True, "backend": self.vault.backend, "credentials": items, "count": len(items)}

    def status(self, credential_id: str) -> dict:
        entry = self._index.get(credential_id)
        if not entry:
            return {"success": False, "error_code": "CREDENTIAL_NOT_FOUND"}
        return {"success": True, **{key: entry[key] for key in ("credential_id", "kind", "description", "updated_at")},
                "has_secret": True, "usable": True}

    # -------------------------------------------------- INTERNAL ONLY (§89/§93)
    def resolve(self, credential_id: str) -> str | None:
        """Return the raw secret to INTERNAL callers only (http client, browser
        adapter, process env, ssh). Never expose through tools/results/logs."""
        document = self.vault.read(credential_id)
        return (document or {}).get("secret")

    def inject_environment(self, credential_id: str, variable: str) -> dict[str, str]:
        """Scoped env fragment for a child process (§93/§94) — caller merges it
        into that process' environment only."""
        secret = self.resolve(credential_id)
        if secret is None:
            raise CredentialError("CREDENTIAL_NOT_FOUND", credential_id)
        return {variable: secret}

    def inject_header(self, credential_id: str, scheme: str = "Bearer",
                      header: str = "Authorization") -> dict[str, str]:
        secret = self.resolve(credential_id)
        if secret is None:
            raise CredentialError("CREDENTIAL_NOT_FOUND", credential_id)
        return {header: f"{scheme} {secret}"}

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _binding_digest(*values: Any) -> str:
        material = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _require_approval(self, *, description: str, resource_key: str, risk: str,
                          approval_id: str | None, binding_digest: str = "") -> dict | None:
        """Returns an APPROVAL_REQUIRED payload when no valid approval is bound."""
        if self.approvals is None:
            return {"success": False, "error_code": "APPROVAL_REQUIRED", "approval_required": True}
        approval_command = f"{description} params_sha256={binding_digest or 'none'}"
        fingerprint = self.approvals.fingerprint(
            approval_command, "credential_broker", "", 30, target="local",
        )
        if not approval_id:
            record = self.approvals.request(
                command=approval_command, shell="credential_broker", working_directory="",
                timeout_seconds=30, risk_level=ShellRiskLevel(risk), target="local",
                fingerprint=fingerprint,
            )
            return {"success": False, "error_code": "APPROVAL_REQUIRED", "approval_required": True,
                    "approval_id": record.approval_id}
        granted, reason = self.approvals.consume(approval_id, fingerprint)
        if not granted:
            return {"success": False, "error_code": "APPROVAL_INVALID", "message": reason}
        return None


def assert_no_leak(payload: Any, secret: str) -> None:
    """Test/CI helper (§96): recursively assert a raw secret never appears."""
    import json as _json

    serialized = _json.dumps(payload, ensure_ascii=False, default=str)
    if secret and secret in serialized:
        raise AssertionError("SECRET LEAK DETECTED em payload do credential broker")


__all__ = [
    "CredentialBroker",
    "CredentialError",
    "CredentialVault",
    "assert_no_leak",
]
