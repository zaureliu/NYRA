"""Programatic password SSH transport (openwrt-fix.md).

O ``ssh.exe`` em modo ``BatchMode=yes`` nunca consegue consumir a senha salva
no Credential Broker — é exatamente por isso que a integração OpenWrt caía em
``AUTH_FAILED / REMOTE_AUTH_FAILED`` com a MESMA credencial que funciona no
login manual. Este módulo implementa o elo que faltava:

    UI → Credential Broker → AsyncSSH (password auth) → root@host → ubus.

Regras de segurança preservadas:

    * a senha vive SOMENTE no Credential Broker e é passada em memória para o
      asyncssh; ela NUNCA aparece em logs, resultados, histórico, auditoria ou
      exceções (todas as saídas usam marcadores sintéticos);
    * host key: exige o ``known_hosts_path`` pré-cadastrado no Trusted Host
      Registry. A senha nunca é enviada antes de a identidade ser validada;
    * timeouts explícitos e curtos para connect/auth/comando.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from app.core.paths import DATA_ROOT
from app.tools.remote_executor import RawRemoteResult

logger = logging.getLogger("kazumi.remote_password_ssh")

EXECUTABLE_LABEL = "asyncssh(password)"

# Marcadores sintéticos compatíveis com o mapeamento de evidência do
# RemoteShellService._result_from_raw — a senha real jamais é serializada.
_AUTH_DENIED_MARKER = b"Permission denied (password rejected by host)."
_HOST_KEY_CHANGED_MARKER = b"REMOTE HOST IDENTIFICATION HAS CHANGED! (strict known_hosts)"
_CONNECT_TIMEOUT_MARKER = b"Connection timed out during SSH connect."
_CONNECT_FAILED_MARKER = b"Connection to host failed."


class HostKeyTofuStore:
    """Leitor legado para exportação manual; nunca concede confiança em conexão."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._cache is None:
            try:
                document = json.loads(self.path.read_text(encoding="utf-8"))
                self._cache = document if isinstance(document, dict) else {}
            except (OSError, ValueError):
                self._cache = {}
        return self._cache

    def _save(self) -> None:
        assert self._cache is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    @staticmethod
    def _key(address: str, port: int) -> str:
        return f"{address}:{port}"

    def known_keys(self, address: str, port: int) -> dict[str, Any]:
        entry = self._load().get(self._key(address, port)) or {}
        return {str(algo): item for algo, item in (entry.get("keys") or {}).items()}

    def record(self, address: str, port: int, algorithm: str, fingerprint: str,
               public_key: str) -> None:
        document = self._load()
        key = self._key(address, port)
        entry = document.get(key)
        if not isinstance(entry, dict):
            entry = {"address": address[:253], "port": int(port),
                     "first_trusted_at": time.time(), "keys": {}}
        keys = entry.setdefault("keys", {})
        if algorithm not in keys:
            keys[algorithm] = {"fingerprint_sha256": fingerprint,
                               "public_key": public_key[:600],
                               "trusted_at": time.time()}
        else:
            keys[algorithm]["last_seen_at"] = time.time()
        entry["last_seen_at"] = time.time()
        document[key] = entry
        self._save()

    def matches(self, address: str, port: int, algorithm: str,
                fingerprint: str) -> bool | None:
        """True se confia; None se ainda não há registro (primeiro contato); False se diverge."""
        stored = self.known_keys(address, port).get(algorithm)
        if stored is None:
            return None if not self.known_keys(address, port) else False
        return bool(stored.get("fingerprint_sha256") == fingerprint)

    def export_openssh_known_hosts(self, address: str, port: int,
                                   destination: Path) -> Path:
        """Regenera um arquivo known_hosts OpenSSH a partir das fingerprints
        persistidas — o asyncssh valida por ele; o usuário NUNCA é consultado."""
        lines = []
        for algorithm, item in sorted(self.known_keys(address, port).items()):
            public_key = str(item.get("public_key") or "").strip()
            if public_key:
                host_pattern = f"{address},{address}:{port}"
                lines.append(f"{host_pattern} {public_key}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="ascii")
        return destination


class AsyncSSHPasswordExecutor:
    """Execução SSH programática com autenticação por senha (asyncssh)."""

    def __init__(self, store_path: Path | None = None) -> None:
        self.store_path = store_path or DATA_ROOT / "ssh-host-keys"

    def store_for(self, host_id: str) -> HostKeyTofuStore:
        safe = "".join(char for char in host_id.lower() if char.isalnum() or char in "-_") or "default"
        return HostKeyTofuStore(self.store_path / f"{safe}.json")

    async def execute(
        self,
        *,
        host_id: str,
        address: str,
        port: int,
        username: str,
        password: str,
        command: str,
        connect_timeout_seconds: float,
        command_timeout_seconds: float,
        known_hosts_path: Path,
    ) -> RawRemoteResult:
        started = time.perf_counter()

        def duration_ms() -> float:
            return round((time.perf_counter() - started) * 1000, 2)

        try:
            import asyncssh  # noqa: PLC0415 - import tardio: dependência opcional
        except ImportError:
            logger.warning("asyncssh_unavailable")
            return RawRemoteResult(EXECUTABLE_LABEL, None, b"", b"", duration_ms(),
                                   launch_error="asyncssh is unavailable")

        try:
            connection = await asyncio.wait_for(
                asyncssh.connect(
                    address,
                    port=port,
                    username=username,
                    password=password,
                    client_keys=None,
                    agent_path=None,
                    known_hosts=str(known_hosts_path),
                    login_timeout=max(1.0, float(connect_timeout_seconds)),
                    connect_timeout=max(1.0, float(connect_timeout_seconds)),
                ),
                max(1.0, float(connect_timeout_seconds)),
            )
        except asyncssh.HostKeyNotVerifiable:
            return RawRemoteResult(EXECUTABLE_LABEL, 255, b"", _HOST_KEY_CHANGED_MARKER,
                                   duration_ms())
        except asyncssh.PermissionDenied:
            return RawRemoteResult(EXECUTABLE_LABEL, 255, b"", _AUTH_DENIED_MARKER,
                                   duration_ms())
        except TimeoutError:
            return RawRemoteResult(EXECUTABLE_LABEL, 255, b"", _CONNECT_TIMEOUT_MARKER,
                                   duration_ms())
        except (OSError, asyncssh.Error, asyncssh.DisconnectError):
            return RawRemoteResult(EXECUTABLE_LABEL, 255, b"", _CONNECT_FAILED_MARKER,
                                   duration_ms())

        try:
            try:
                process = await asyncio.wait_for(
                    connection.run(command, request_pty=False, check=False),
                    max(1.0, float(command_timeout_seconds)),
                )
            except TimeoutError:
                connection.close()
                return RawRemoteResult(EXECUTABLE_LABEL, None, b"", b"",
                                       duration_ms(), timed_out=True)
            stdout = (process.stdout or "").encode("utf-8", "replace")
            stderr = (process.stderr or "").encode("utf-8", "replace")
            exit_code = process.exit_status if process.exit_status is not None else 255
            if process.exit_signal is not None:
                signal_name = process.exit_signal[0]
                stderr = stderr or signal_name.encode("utf-8", "replace")
                exit_code = exit_code or 1
            return RawRemoteResult(EXECUTABLE_LABEL, exit_code, stdout, stderr,
                                   duration_ms())
        finally:
            connection.close()
