"""Local Operator: structured filesystem / process / service / registry / task
capabilities with ACT→VERIFY semantics and honest error codes.

Mutations that Windows gates behind admin rights are routed through the
legitimate Elevated Broker (UAC consent) after a single-use approval record;
no bypass, no credential handling (prompt8 §137-§146). KAZUMI's own components
are protected from indiscriminate stops (§281-§285).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path

import psutil

from app.agent.context import current_agent_run_id
from app.desktop.control import operation_result
from app.events import EventBus, EventType
from app.tools.elevated_broker import run_elevated, is_access_denied_output
from app.tools.shell_approval import ShellApprovalGate

logger = logging.getLogger("kazumi.operator")

_MAX_LIST = 400
_MAX_READ_BYTES = 256_000
_PROTECTED_NAMES = {"kazumi-backend", "kazumi-desktop", "kazumi-frontend"}


def _is_protected_process(process: psutil.Process) -> bool:
    try:
        name = (process.name() or "").casefold()
        exe = (process.exe() or "").casefold()
    except Exception:  # noqa: BLE001
        return False
    if "kazumi" in name or "kazumi" in exe:
        return True
    try:
        parent = psutil.Process(os.getpid()).parent()
        while parent is not None:
            if parent.pid == process.pid:
                return True
            parent = parent.parent()
    except Exception:  # noqa: BLE001
        pass
    return False


class OperatorController:
    def __init__(self, event_bus: EventBus, approvals: ShellApprovalGate) -> None:
        self.event_bus = event_bus
        self.approvals = approvals

    async def _approval(self, action: str, target: str, risk: str, approval_id: str | None,
                        description: str, timeout_seconds: int = 120,
                        binding_digest: str = "") -> tuple[bool, dict | None]:
        """Single-use approval flow; returns (granted, error_payload)."""
        agent_run_id = current_agent_run_id.get()
        binding = binding_digest or "none"
        bound_description = f"{description} [params_sha256={binding[:16]}]"
        fingerprint = self.approvals.fingerprint(
            f"{action}:{target}:{binding}", "local_operator", os.getcwd(), timeout_seconds,
            target="local", agent_run_id=agent_run_id,
        )
        if approval_id:
            granted, rejection = self.approvals.consume(approval_id, fingerprint)
            if not granted:
                return False, {"success": False, "error_code": "APPROVAL_REJECTED",
                               "message": rejection or "Approval inválido para esta ação.", "approval_required": True}
            return True, None
        record = self.approvals.request(
            command=bound_description, shell="local_operator", working_directory=os.getcwd(),
            timeout_seconds=timeout_seconds, risk_level=risk, target=target, agent_run_id=agent_run_id,
            fingerprint=fingerprint,
        )
        await self.event_bus.publish(
            EventType.SHELL_APPROVAL_REQUIRED,
            approval_id=record.approval_id, agent_run_id=agent_run_id,
            command=bound_description, shell="local_operator",
            risk_level=risk, reason=f"local operator {action}", turn_id=None,
        )
        return False, {
            "success": False, "error_code": "APPROVAL_REQUIRED",
            "message": f"Ação '{description}' exige aprovação explícita do operador antes da execução.",
            "approval_required": True, "approval_id": record.approval_id,
        }

    @staticmethod
    def _binding_digest(*values: object) -> str:
        payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _run_elevated_command(powershell_statement: str, timeout_seconds: int = 60) -> dict:
        raw = run_elevated(powershell_statement, "powershell", timeout_seconds, Path.cwd())
        if raw.get("launch_error") == "UAC_CANCELLED":
            return {"ok": False, "code": "ELEVATION_DENIED", "message": "O operador cancelou o prompt UAC."}
        if raw.get("timed_out"):
            return {"ok": False, "code": "ELEVATION_TIMEOUT", "message": "Elevação não respondeu a tempo."}
        combined = f"{raw.get('stdout', '')}\n{raw.get('stderr', '')}"
        if is_access_denied_output(raw.get("stdout", ""), raw.get("stderr", "")):
            return {"ok": False, "code": "ELEVATION_DENIED", "message": "Acesso negado mesmo após elevação."}
        return {"ok": raw.get("exit_code") == 0, "code": None if raw.get("exit_code") == 0 else "COMMAND_FAILED",
                "stdout": raw.get("stdout", "")[:8000], "stderr": raw.get("stderr", "")[:4000],
                "exit_code": raw.get("exit_code"), "message": ""}

    # ------------------------------------------------------------------ filesystem

    async def fs_list(self, path: str, limit: int = 100) -> dict:
        started = time.perf_counter()
        clean = Path(path.strip().strip('"')).expanduser()
        if not clean.is_absolute():
            clean = Path.cwd() / clean
        resolved = clean.resolve()
        if not resolved.exists():
            return operation_result(app="fs", action="list", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="PATH_NOT_FOUND",
                                    message=f"Caminho inexistente: {resolved}")
        entries: list[dict] = []
        try:
            iterator = sorted(resolved.iterdir(), key=lambda item: (item.is_file(), item.name.casefold()))
        except OSError as exc:
            return operation_result(app="fs", action="list", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="FS_ACCESS_DENIED", message=str(exc)[:160])
        for item in iterator[: max(1, min(limit, _MAX_LIST))]:
            entry: dict = {"name": item.name, "type": "dir" if item.is_dir() else "file"}
            if item.is_file():
                try:
                    stat = item.stat()
                    entry["size_bytes"] = stat.st_size
                    entry["modified"] = stat.st_mtime
                except OSError:
                    pass
            entries.append(entry)
        return operation_result(app="fs", action="list", duration_ms=(time.perf_counter() - started) * 1000,
                                success=True, effect_verified=True, verification_status="VERIFIED",
                                detail={"path": str(resolved), "count": len(entries), "entries": entries})

    async def fs_read(self, path: str) -> dict:
        started = time.perf_counter()
        clean = Path(path.strip().strip('"')).expanduser()
        if not clean.is_absolute():
            clean = Path.cwd() / clean
        resolved = clean.resolve()
        if not resolved.is_file():
            return operation_result(app="fs", action="read", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="FILE_NOT_FOUND", message=f"Arquivo inexistente: {resolved}")
        try:
            data = resolved.read_bytes()[:_MAX_READ_BYTES]
            text = data.decode("utf-8")
            encoding_used = "utf-8"
        except UnicodeDecodeError:
            encoding_used = "latin-1"
            text = resolved.read_bytes()[:_MAX_READ_BYTES].decode("latin-1", errors="replace")
        except OSError as exc:
            return operation_result(app="fs", action="read", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="FS_ACCESS_DENIED", message=str(exc)[:160])
        truncated = resolved.stat().st_size > _MAX_READ_BYTES
        return operation_result(app="fs", action="read", duration_ms=(time.perf_counter() - started) * 1000,
                                success=True, effect_verified=True, verification_status="VERIFIED",
                                detail={"path": str(resolved), "encoding": encoding_used,
                                        "truncated": truncated, "content": text})

    async def fs_write(self, path: str, content: str, *, append: bool = False, approval_id: str | None = None) -> dict:
        started = time.perf_counter()
        clean = Path(path.strip().strip('"')).expanduser()
        if not clean.is_absolute():
            clean = Path.cwd() / clean
        resolved = clean.resolve()
        granted, error = await self._approval(
            "fs_write", str(resolved), "LOW_RISK", approval_id,
            f"{'anexar em' if append else 'escrever'} {resolved.name}",
            binding_digest=self._binding_digest(bool(append), content),
        )
        if not granted and error is None:
            error = {"success": False, "error_code": "APPROVAL_REJECTED"}
        if error:
            return operation_result(app="fs", action="write", duration_ms=(time.perf_counter() - started) * 1000, **error)
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with open(resolved, mode, encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            return operation_result(app="fs", action="write", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="FS_WRITE_FAILED", message=str(exc)[:200])
        exists = resolved.is_file()
        size = resolved.stat().st_size if exists else 0
        verified = exists and (size > 0 or content == "")
        digest = ""
        if exists and not append:
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        return operation_result(app="fs", action="write", duration_ms=(time.perf_counter() - started) * 1000,
                                success=True, error_code=None if verified else "VERIFY_FAILED",
                                execution_success=True, effect_verified=verified,
                                verification_status="VERIFIED" if verified else "VERIFICATION_FAILED",
                                message=f"Arquivo {'anexado' if append else 'escrito'}: {resolved.name} ({size} bytes).",
                                detail={"path": str(resolved), "size_bytes": size, "sha16": digest})

    async def fs_copy(self, source: str, destination: str, approval_id: str | None = None) -> dict:
        return await self._fs_transfer("copy", source, destination, approval_id)

    async def fs_move(self, source: str, destination: str, approval_id: str | None = None) -> dict:
        return await self._fs_transfer("move", source, destination, approval_id)

    async def _fs_transfer(self, action: str, source: str, destination: str, approval_id: str | None) -> dict:
        started = time.perf_counter()

        def normalize(value: str) -> Path:
            clean = Path(value.strip().strip('"')).expanduser()
            return (Path.cwd() / clean).resolve() if not clean.is_absolute() else clean.resolve()

        src, dst = normalize(source), normalize(destination)
        granted, error = await self._approval(action, f"{src} -> {dst}", "LOW_RISK", approval_id,
                                              f"{action} {src.name} -> {dst.name}")
        if error:
            return operation_result(app="fs", action=action, duration_ms=(time.perf_counter() - started) * 1000, **error)
        if not src.exists():
            return operation_result(app="fs", action=action, duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="FILE_NOT_FOUND", message=f"Origem inexistente: {src}")
        try:
            final_dst = dst / src.name if dst.is_dir() else dst
            if action == "copy":
                result_path = shutil_copy(src, final_dst)
            else:
                result_path = shutil_move(src, final_dst)
        except OSError as exc:
            return operation_result(app="fs", action=action, duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="FS_TRANSFER_FAILED", message=str(exc)[:200])
        verified = result_path.exists() and (not src.exists() if action == "move" else True)
        return operation_result(app="fs", action=action, duration_ms=(time.perf_counter() - started) * 1000,
                                success=True, execution_success=True, effect_verified=verified,
                                verification_status="VERIFIED" if verified else "VERIFICATION_FAILED",
                                message=f"{action.capitalize()} concluído: {result_path}",
                                detail={"source": str(src), "destination": str(result_path)})

    async def fs_rename(self, path: str, new_name: str, approval_id: str | None = None) -> dict:
        started = time.perf_counter()
        clean = Path(path.strip().strip('"')).expanduser()
        resolved = (Path.cwd() / clean).resolve() if not clean.is_absolute() else clean.resolve()
        safe_name = Path(new_name.strip().strip('"')).name
        granted, error = await self._approval("rename", str(resolved), "LOW_RISK", approval_id,
                                              f"renomear {resolved.name} -> {safe_name}",
                                              binding_digest=self._binding_digest(safe_name))
        if error:
            return operation_result(app="fs", action="rename", duration_ms=(time.perf_counter() - started) * 1000, **error)
        if not resolved.exists():
            return operation_result(app="fs", action="rename", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="FILE_NOT_FOUND", message=f"Inexistente: {resolved}")
        target = resolved.with_name(safe_name)
        try:
            resolved.rename(target)
        except OSError as exc:
            return operation_result(app="fs", action="rename", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="FS_RENAME_FAILED", message=str(exc)[:200])
        verified = target.exists() and not resolved.exists()
        return operation_result(app="fs", action="rename", duration_ms=(time.perf_counter() - started) * 1000,
                                success=True, execution_success=True, effect_verified=verified,
                                verification_status="VERIFIED" if verified else "VERIFICATION_FAILED",
                                message=f"Renomeado para {target}",
                                detail={"old": str(resolved), "new": str(target)})

    async def fs_delete(self, path: str, approval_id: str | None = None, reason: str = "") -> dict:
        started = time.perf_counter()
        clean = Path(path.strip().strip('"')).expanduser()
        resolved = (Path.cwd() / clean).resolve() if not clean.is_absolute() else clean.resolve()
        if str(resolved).casefold() in {str(Path.cwd().resolve()).casefold(), str(Path.home()).casefold()} \
                or len(resolved.parts) <= 2:
            return operation_result(app="fs", action="delete", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="PROTECTED_PATH",
                                    message="Exclusão bloqueada por política: caminho raiz/home/projeto.",
                                    execution_success=False)
        granted, error = await self._approval("fs_delete", str(resolved), "DESTRUCTIVE", approval_id,
                                              f"excluir {resolved.name}", timeout_seconds=180)
        if error:
            return operation_result(app="fs", action="delete", duration_ms=(time.perf_counter() - started) * 1000, **error)
        try:
            if resolved.is_dir():
                shutil_rmtree(resolved)
            else:
                resolved.unlink()
        except OSError as exc:
            return operation_result(app="fs", action="delete", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="FS_DELETE_FAILED", message=str(exc)[:200])
        verified = not resolved.exists()
        return operation_result(app="fs", action="delete", duration_ms=(time.perf_counter() - started) * 1000,
                                success=True, execution_success=True, effect_verified=verified,
                                verification_status="VERIFIED" if verified else "VERIFICATION_FAILED",
                                message=f"Removido: {resolved}" if verified else "Ainda presente após exclusão.",
                                detail={"path": str(resolved), "reason": reason[:200]})

    async def fs_mkdir(self, path: str, approval_id: str | None = None) -> dict:
        started = time.perf_counter()
        clean = Path(path.strip().strip('"')).expanduser()
        resolved = (Path.cwd() / clean).resolve() if not clean.is_absolute() else clean.resolve()
        granted, error = await self._approval("mkdir", str(resolved), "LOW_RISK", approval_id, f"criar pasta {resolved.name}")
        if error:
            return operation_result(app="fs", action="mkdir", duration_ms=(time.perf_counter() - started) * 1000, **error)
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return operation_result(app="fs", action="mkdir", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="FS_MKDIR_FAILED", message=str(exc)[:200])
        verified = resolved.is_dir()
        return operation_result(app="fs", action="mkdir", duration_ms=(time.perf_counter() - started) * 1000,
                                success=True, execution_success=True, effect_verified=verified,
                                verification_status="VERIFIED" if verified else "VERIFICATION_FAILED",
                                message=f"Diretório pronto: {resolved}", detail={"path": str(resolved)})

    async def fs_search(self, root: str, pattern: str, limit: int = 50) -> dict:
        import fnmatch

        started = time.perf_counter()
        base = Path(root.strip().strip('"')).expanduser()
        if not base.is_absolute():
            base = Path.cwd() / base
        base = base.resolve()
        needle = pattern.strip()
        if not base.is_dir() or len(needle) < 2:
            return operation_result(app="fs", action="search", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="INVALID_SEARCH",
                                    message="Raiz inválida ou padrão muito curto.")
        matches: list[str] = []
        low_needle = needle.casefold()
        try:
            for current_root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if not d.startswith(".")][:60]
                for candidate in list(dirs) + files:
                    if fnmatch.fnmatch(candidate.casefold(), f"*{low_needle}*") or low_needle in candidate.casefold():
                        matches.append(str(Path(current_root) / candidate))
                        if len(matches) >= min(limit, 100):
                            break
                if len(matches) >= min(limit, 100):
                    break
        except OSError as exc:
            return operation_result(app="fs", action="search", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="FS_ACCESS_DENIED", message=str(exc)[:160])
        return operation_result(app="fs", action="search", duration_ms=(time.perf_counter() - started) * 1000,
                                success=True, effect_verified=True, verification_status="VERIFIED",
                                detail={"root": str(base), "pattern": needle, "count": len(matches),
                                        "matches": matches})

    # ------------------------------------------------------------------ processes

    async def process_list(self, sort_by: str = "memory", limit: int = 25) -> dict:
        started = time.perf_counter()
        processes: list[dict] = []
        for process in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent", "status", "create_time"]):
            try:
                info = process.info
                memory = info.get("memory_info")
                processes.append({
                    "pid": info["pid"],
                    "name": info["name"],
                    "memory_mb": round((memory.rss if memory else 0) / (1024 * 1024), 1),
                    "status": info.get("status"),
                    "protected": _is_protected_process(process),
                })
            except Exception:  # noqa: BLE001
                continue
        key = (lambda item: item["memory_mb"]) if sort_by.casefold().startswith("mem") else (lambda item: item["pid"])
        processes.sort(key=key, reverse=sort_by.casefold().startswith("mem"))
        total_memory = sum(item["memory_mb"] for item in processes)
        return operation_result(app="process", action="list", duration_ms=(time.perf_counter() - started) * 1000,
                                success=True, effect_verified=True, verification_status="VERIFIED",
                                detail={"count": len(processes), "total_memory_mb": round(total_memory, 1),
                                        "processes": processes[: max(1, min(limit, 80))]})

    async def process_status(self, pid: int | None = None, name: str = "") -> dict:
        started = time.perf_counter()
        found: list[dict] = []
        if pid:
            try:
                candidates = [psutil.Process(int(pid))]
            except (psutil.NoSuchProcess, ValueError):
                candidates = []
        elif name.strip():
            needle = name.strip().casefold().removesuffix(".exe")
            candidates = [p for p in psutil.process_iter(["name"]) if needle in (p.info.get("name") or "").casefold()]
        else:
            return operation_result(app="process", action="status", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="TARGET_REQUIRED", message="Informe pid ou name.")
        for process in candidates[:10]:
            try:
                found.append({
                    "pid": process.pid, "name": process.name(),
                    "exe": process.exe(), "cmdline": " ".join(process.cmdline() or [])[:300],
                    "create_time": process.create_time(),
                    "memory_mb": round(process.memory_info().rss / (1024 * 1024), 1),
                    "status": process.status(), "protected": _is_protected_process(process),
                })
            except Exception:  # noqa: BLE001
                continue
        return operation_result(app="process", action="status", duration_ms=(time.perf_counter() - started) * 1000,
                                success=bool(found), error_code=None if found else "PROCESS_NOT_FOUND",
                                message="" if found else "Nenhum processo correspondente.",
                                effect_verified=bool(found), verification_status="VERIFIED" if found else "NOT_EXECUTED",
                                detail={"processes": found})

    async def process_start(self, executable: str, arguments: str = "", approval_id: str | None = None) -> dict:
        started = time.perf_counter()
        return operation_result(app="process", action="start", duration_ms=(time.perf_counter() - started) * 1000,
                                success=False, execution_success=False,
                                error_code="PROCESS_START_REQUIRES_SYSTEM_SHELL",
                                verification_status="NOT_EXECUTED",
                                message="Inicialização arbitrária de processos só é permitida por system_shell.")

    async def process_stop(self, pid: int, approval_id: str | None = None, force: bool = False, reason: str = "") -> dict:
        started = time.perf_counter()
        try:
            process = psutil.Process(int(pid))
        except (psutil.NoSuchProcess, ValueError):
            return operation_result(app="process", action="stop", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="PROCESS_NOT_FOUND", message=f"PID {pid} não existe.",
                                    execution_success=False)
        if _is_protected_process(process):
            return operation_result(app="process", action="stop", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="PROTECTED_PROCESS",
                                    message=f"'{process.name()}' é componente da própria KAZUMI; parada bloqueada. Peça explicitamente ao operador um fluxo administrativo.",
                                    execution_success=False)
        identity = {"pid": process.pid, "name": process.name()}
        granted, error = await self._approval(
            "process_stop", f"{identity['pid']}:{identity['name']}", "ELEVATED", approval_id,
            f"parar processo {identity['name']} (pid {identity['pid']})", timeout_seconds=180,
            binding_digest=self._binding_digest(bool(force)),
        )
        if error:
            payload = operation_result(app="process", action="stop", duration_ms=(time.perf_counter() - started) * 1000, **error)
            payload.update({"target": identity})
            return payload
        try:
            if force:
                process.kill()
            else:
                process.terminate()
            process.wait(timeout=8)
        except psutil.TimeoutExpired:
            if not force:
                try:
                    process.kill()
                    process.wait(timeout=4)
                except Exception:  # noqa: BLE001
                    pass
        except psutil.NoSuchProcess:
            pass
        except Exception as exc:  # noqa: BLE001
            return operation_result(app="process", action="stop", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="PROCESS_STOP_FAILED", message=str(exc)[:200])
        gone = not psutil.pid_exists(int(pid))
        return operation_result(app="process", action="stop", duration_ms=(time.perf_counter() - started) * 1000,
                                success=gone, execution_success=True, effect_verified=gone,
                                verification_status="VERIFIED" if gone else "VERIFICATION_FAILED",
                                message=f"Processo {identity['name']} (pid {pid}) {'encerrado' if gone else 'ainda ativo'}.",
                                detail={**identity, "reason": reason[:200]})

    # ------------------------------------------------------------------ services

    async def service_list(self, status_filter: str = "", limit: int = 40) -> dict:
        started = time.perf_counter()
        services: list[dict] = []
        try:
            for service in psutil.win_service_iter():
                info = service.as_dict()
                state = (info.get("status") or "")
                if status_filter.strip() and status_filter.strip().casefold() not in state.casefold():
                    continue
                services.append({
                    "name": info.get("name"), "display_name": info.get("display_name"),
                    "status": state, "start_type": info.get("start_type"),
                })
        except Exception as exc:  # noqa: BLE001
            return operation_result(app="service", action="list", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="SERVICE_ENUM_FAILED", message=str(exc)[:160])
        services.sort(key=lambda item: (item["name"] or "").casefold())
        return operation_result(app="service", action="list", duration_ms=(time.perf_counter() - started) * 1000,
                                success=True, effect_verified=True, verification_status="VERIFIED",
                                detail={"count": len(services), "services": services[: max(1, min(limit, 120))]})

    async def service_action(self, action: str, name: str, approval_id: str | None = None, reason: str = "") -> dict:
        """start/stop/restart via legitimate UAC elevation after operator approval."""
        started = time.perf_counter()
        safe_name = "".join(ch for ch in name.strip() if ch.isalnum() or ch in "-_. ")[:80]
        if not safe_name:
            return operation_result(app="service", action=action, duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="TARGET_REQUIRED", message="Informe o nome do serviço.")
        statement_map = {
            "start": f"Start-Service -Name '{safe_name}' -ErrorAction Stop; (Get-Service -Name '{safe_name}').Status",
            "stop": f"Stop-Service -Name '{safe_name}' -Force -ErrorAction Stop; (Get-Service -Name '{safe_name}').Status",
            "restart": f"Restart-Service -Name '{safe_name}' -Force -ErrorAction Stop; (Get-Service -Name '{safe_name}').Status",
        }
        statement = statement_map.get(action)
        if statement is None:
            return operation_result(app="service", action=action, duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="INVALID_ACTION", message="Use start, stop ou restart.")
        expected = {"start": "RUNNING", "stop": "STOPPED", "restart": None}[action]
        granted, error = await self._approval(f"service_{action}", safe_name, "ELEVATED", approval_id,
                                              f"{action} serviço {safe_name}", timeout_seconds=180)
        if error:
            return operation_result(app="service", action=action, duration_ms=(time.perf_counter() - started) * 1000, **error)
        result = await asyncio_to_thread(self._run_elevated_command, statement, 90)
        if not result.get("ok"):
            code = result.get("code") or "SERVICE_ACTION_FAILED"
            message = result.get("message") or (result.get("stderr") or result.get("stdout") or "")[:200] or f"Falha ao {action} serviço."
            return operation_result(app="service", action=action, duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code=code, message=message,
                                    execution_success=False, effect_verified=False)
        observed = (result.get("stdout") or "").strip()
        verified = True if expected is None else expected.casefold() in observed.casefold()
        return operation_result(app="service", action=action, duration_ms=(time.perf_counter() - started) * 1000,
                                success=True, execution_success=True, effect_verified=verified,
                                verification_status="VERIFIED" if verified else "VERIFICATION_FAILED",
                                message=f"Serviço {safe_name}: estado observado '{observed[:30]}'.",
                                detail={"service": safe_name, "observed_state": observed[:40], "reason": reason[:200]})

    # ------------------------------------------------------------------ registry

    async def registry_read(self, key_path: str, value_name: str = "") -> dict:
        started = time.perf_counter()
        hive, rest = _split_hive(key_path)
        if hive is None:
            return operation_result(app="registry", action="read", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="INVALID_KEY",
                                    message="Caminho deve iniciar com HKLM\\, HKCU\\, HKCR\\, HKU\\ ou HKCC\\.")
        args = ["reg", "query", f"{hive}\\{rest}"]
        if value_name.strip():
            args += ["/v", value_name.strip()]
        try:
            completed = await asyncio_to_thread(
                subprocess.run, args, capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return operation_result(app="registry", action="read", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="REG_TIMEOUT", message="Consulta excedeu o timeout.")
        stdout = _decode_console(completed.stdout)
        stderr = _decode_console(completed.stderr)
        if completed.returncode != 0:
            code = "REG_KEY_NOT_FOUND" if "unable to find" in stderr.casefold() else "REG_QUERY_FAILED"
            return operation_result(app="registry", action="read", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code=code, message=stderr[:200])
        return operation_result(app="registry", action="read", duration_ms=(time.perf_counter() - started) * 1000,
                                success=True, effect_verified=True, verification_status="VERIFIED",
                                detail={"key": f"{hive}\\{rest}", "output": stdout[:6000]})

    async def registry_set(self, key_path: str, value_name: str, value: str, reg_type: str = "REG_SZ",
                           approval_id: str | None = None, reason: str = "") -> dict:
        started = time.perf_counter()
        hive, rest = _split_hive(key_path)
        if hive is None:
            return operation_result(app="registry", action="set", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="INVALID_KEY", message="Hive inválida.")
        allowed_types = {"REG_SZ", "REG_DWORD", "REG_QWORD", "REG_BINARY", "REG_EXPAND_SZ"}
        safe_type = reg_type.strip().upper()
        if safe_type not in allowed_types:
            return operation_result(app="registry", action="set", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="INVALID_TYPE", message=f"Tipo deve ser um de {sorted(allowed_types)}.")
        safe_value_name = "".join(ch for ch in value_name.strip() if ch.isalnum() or ch in "-_ ()")[:64]
        if not safe_value_name:
            return operation_result(app="registry", action="set", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="TARGET_REQUIRED", message="Nome do valor obrigatório.")
        if any(ch in value for ch in "\r\n"):
            return operation_result(app="registry", action="set", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="INVALID_VALUE", message="Valor contém quebra de linha.")
        backup = await self.registry_read(key_path, safe_value_name)
        previous = (backup.get("detail") or {}).get("output", "")[:1500] if backup.get("success") else ""
        granted, error = await self._approval("registry_set", f"{hive}\\{rest}\\{safe_value_name}", "ELEVATED", approval_id,
                                              f"definir {safe_value_name} em {hive}\\{rest}", timeout_seconds=180,
                                              binding_digest=self._binding_digest(safe_type, value))
        if error:
            return operation_result(app="registry", action="set", duration_ms=(time.perf_counter() - started) * 1000, **error)
        full_key = f"{hive}\\{rest}"
        statement = (
            f"New-Item -Path 'Registry::{full_key}' -Force | Out-Null; "
            f"Set-ItemProperty -Path 'Registry::{full_key}' -Name '{_ps_escape(safe_value_name)}' "
            f"-Value '{_ps_escape(value)}' -Type {safe_type}; "
            f"(Get-ItemProperty -Path 'Registry::{full_key}' -Name '{_ps_escape(safe_value_name)}')."
            f"'{_ps_escape(safe_value_name)}'"
        )
        result = await asyncio_to_thread(self._run_elevated_command, statement, 90)
        if not result.get("ok"):
            return operation_result(app="registry", action="set", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code=result.get("code") or "REG_SET_FAILED",
                                    message=result.get("message") or (result.get("stderr") or "")[:200],
                                    execution_success=False, detail={"previous_value_backup": previous})
        readback = await self.registry_read(key_path, safe_value_name)
        verified = readback.get("success") is True and value[:40] in ((readback.get("detail") or {}).get("output", ""))
        return operation_result(app="registry", action="set", duration_ms=(time.perf_counter() - started) * 1000,
                                success=True, execution_success=True, effect_verified=verified,
                                verification_status="VERIFIED" if verified else "VERIFICATION_FAILED",
                                message=f"Valor '{safe_value_name}' gravado e {'relido' if verified else 'não pôde ser relido'}.",
                                detail={"key": full_key, "value_name": safe_value_name, "previous_value_backup": previous,
                                        "reason": reason[:200]})

    # ------------------------------------------------------------------ power / session

    async def system_power(self, action: str, approval_id: str | None = None, reason: str = "") -> dict:
        """lock/sleep/logoff/restart/shutdown — explicit operator intent required."""
        started = time.perf_counter()
        spec = {
            # action: (risk, description, argv)
            "lock": ("LOW_RISK", "bloquear a estação de trabalho",
                     ["rundll32.exe", "user32.dll,LockWorkStation"]),
            "sleep": ("ELEVATED", "colocar o computador em suspensão",
                      ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]),
            "logoff": ("CRITICAL", "encerrar a sessão do usuário",
                       ["shutdown", "/l"]),
            "restart": ("CRITICAL", "REINICIAR o computador (30s para cancelar com shutdown /a)",
                        ["shutdown", "/r", "/t", "30"]),
            "shutdown": ("CRITICAL", "DESLIGAR o computador (30s para cancelar com shutdown /a)",
                         ["shutdown", "/s", "/t", "30"]),
        }.get(action)
        if spec is None:
            return operation_result(app="system", action=action, duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="INVALID_ACTION",
                                    message="Use lock, sleep, logoff, restart ou shutdown.")
        risk, description, argv = spec
        if risk != "LOW_RISK":
            granted, error = await self._approval(f"power_{action}", action, risk, approval_id,
                                                  description, timeout_seconds=180)
            if error:
                return operation_result(app="system", action=action, duration_ms=(time.perf_counter() - started) * 1000, **error)
        else:
            granted, error = True, None
        try:
            process = subprocess.Popen(  # noqa: S603 - argv fixo da tabela acima, sem input concatenado
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            )
        except OSError as exc:
            return operation_result(app="system", action=action, duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="COMMAND_FAILED", message=str(exc)[:160],
                                    execution_success=False)
        return operation_result(app="system", action=action, duration_ms=(time.perf_counter() - started) * 1000,
                                success=True, execution_success=True, effect_verified=None,
                                verification_status="EXECUTED",
                                message=f"Solicitação enviada: {description}.",
                                detail={"reason": reason[:200], "cancel_hint": "shutdown /a" if action in {"restart", "shutdown"} else None})

    def status(self) -> dict:
        return {
            "enabled": True,
            "filesystem": ["list", "read", "write", "copy", "move", "rename", "delete", "mkdir", "search"],
            "processes": ["list", "status", "start", "stop"],
            "services": ["list", "status", "start", "stop", "restart"],
            "registry": ["read", "set"],
            "scheduled_tasks": ["list", "run", "delete"],
            "power": ["lock", "sleep", "logoff", "restart", "shutdown"],
            "protected": "componentes KAZUMI não podem ser parados por tools automáticas",
        }

    # ------------------------------------------------------------------ scheduled tasks

    async def task_list(self, folder: str = "\\") -> dict:
        started = time.perf_counter()
        safe_folder = folder.strip() or "\\"
        if any(ch in safe_folder for ch in "\r\n;&|"):
            return operation_result(app="task", action="list", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="INVALID_FOLDER", message="Pasta inválida.")
        try:
            completed = await asyncio_to_thread(
                subprocess.run,
                ["schtasks", "/query", "/fo", "CSV", "/nh", "/v"],
                capture_output=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return operation_result(app="task", action="list", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="TASK_TIMEOUT", message="Consulta excedeu o timeout.")
        if completed.returncode != 0:
            return operation_result(app="task", action="list", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="TASK_QUERY_FAILED",
                                    message=_decode_console(completed.stderr)[:200])
        import csv
        import io

        text = _decode_console(completed.stdout)
        rows = list(csv.reader(io.StringIO(text)))
        tasks = []
        for row in rows[1:] if rows and rows[0] else []:
            if len(row) >= 2:
                tasks.append({"name": row[0][-90:], "next_run": row[1][:24] if len(row) > 1 else "",
                              "status": row[3][:24] if len(row) > 3 else ""})
        filtered = [item for item in tasks if safe_folder == "\\" or safe_folder.casefold() in item["name"].casefold()]
        return operation_result(app="task", action="list", duration_ms=(time.perf_counter() - started) * 1000,
                                success=True, effect_verified=True, verification_status="VERIFIED",
                                detail={"folder": safe_folder, "count": len(filtered), "tasks": filtered[:80]})

    async def task_action(self, action: str, name: str, approval_id: str | None = None, reason: str = "") -> dict:
        started = time.perf_counter()
        safe_name = name.strip().strip('"')
        if not safe_name or any(ch in safe_name for ch in "\r\n;&|`"):
            return operation_result(app="task", action=action, duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="TARGET_REQUIRED", message="Nome de tarefa inválido.")
        arg_map = {"run": ["/run"], "delete": ["/delete", "/f"]}
        extra = arg_map.get(action)
        if extra is None:
            return operation_result(app="task", action=action, duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="INVALID_ACTION", message="Use run ou delete (criação via schtasks pelo system_shell aprovado).")
        risk = "ELEVATED" if action == "run" else "DESTRUCTIVE"
        granted, error = await self._approval(f"task_{action}", safe_name, risk, approval_id,
                                              f"{action} tarefa agendada {safe_name}", timeout_seconds=180)
        if error:
            return operation_result(app="task", action=action, duration_ms=(time.perf_counter() - started) * 1000, **error)
        try:
            completed = await asyncio_to_thread(
                subprocess.run,
                ["schtasks", *extra, "/tn", safe_name],
                capture_output=True, timeout=25,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return operation_result(app="task", action=action, duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="TASK_TIMEOUT", message="Operação excedeu o timeout.")
        ok = completed.returncode == 0
        output = _decode_console(completed.stdout or completed.stderr)
        if action == "run":
            verify = await asyncio_to_thread(
                subprocess.run, ["schtasks", "/query", "/tn", safe_name, "/fo", "LIST"],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            listing = _decode_console(verify.stdout)
            verified = ok and "Status:" in listing
        else:
            verify = await asyncio_to_thread(
                subprocess.run, ["schtasks", "/query", "/tn", safe_name],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            verified = ok and verify.returncode != 0
        return operation_result(app="task", action=action, duration_ms=(time.perf_counter() - started) * 1000,
                                success=ok, execution_success=ok, effect_verified=verified,
                                verification_status="VERIFIED" if verified else ("EXECUTED" if ok else "EXECUTION_FAILED"),
                                message=output[:220] or ("Concluído." if ok else "Falhou."),
                                detail={"task": safe_name, "reason": reason[:200]})


def shutil_copy(src: Path, dst: Path) -> Path:
    import shutil

    return Path(shutil.copy2(src, dst))


def shutil_move(src: Path, dst: Path) -> Path:
    import shutil

    return Path(shutil.move(str(src), str(dst)))


def shutil_rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path)


async def asyncio_sleep_short() -> None:
    import asyncio

    await asyncio.sleep(0.25)


async def asyncio_to_thread(fn, *args, **kwargs):
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)


_HIVES = {"HKLM": "HKEY_LOCAL_MACHINE", "HKCU": "HKEY_CURRENT_USER", "HKCR": "HKEY_CLASSES_ROOT",
          "HKU": "HKEY_USERS", "HKCC": "HKEY_CURRENT_CONFIG"}


def _split_hive(key_path: str) -> tuple[str | None, str]:
    clean = key_path.strip().strip("/\\")
    parts = clean.split("\\", 1)
    if not parts or parts[0].upper() not in _HIVES:
        return None, ""
    hive = parts[0].upper()
    rest = parts[1].strip("\\") if len(parts) > 1 else ""
    if any(ch in rest for ch in "\r\n;|&"):
        return None, ""
    return hive, rest


def _ps_escape(value: str) -> str:
    return value.replace("'", "''")


def _decode_console(data: bytes | None) -> str:
    if not data:
        return ""
    for encoding in ("utf-8", "cp850", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
