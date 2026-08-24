"""Desktop application lifecycle: launch GUI apps and VERIFY the window is visible.

Launch uses detached spawn with DEVNULL pipes (no console, no pipe deadlocks).
Verification enumerates real top-level windows via Win32; a launch is only
reported as VERIFIED when a matching visible window exists on the desktop.

V2: beyond the trusted registry, a Dynamic Application Resolver locates any
installed app (App Paths, PATH, Start Menu, Get-StartApps) when dynamic
discovery is enabled. Unknown or failed launches return tool-shaped results
instead of exploding the conversation pipeline.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from app.core.paths import CONFIG_ROOT
from app.core.turn import get_current_turn_id
from app.desktop.apps import DesktopAppsRegistry, load_desktop_apps
from app.desktop.discovery import (
    ApplicationCandidate,
    ApplicationDiscovery,
    LaunchMethod,
    expand_launch_target,
    normalize,
)
from app.desktop.models import (
    DesktopAppSpec,
    LaunchErrorCode,
    WindowInfo,
    operation_result,
)
from app.desktop.windows import annotate_process_names, find_windows_for_app, list_visible_windows
from app.events import EventBus, EventType

logger = logging.getLogger("nyra.desktop")

_BACKGROUND_HINTS = {"steam", "discord", "onedrive", "dropbox"}


def redact_query(query: str) -> str:
    return " ".join(query.split())[:80]


def _is_protected_window(window: WindowInfo) -> bool:
    """NYRA components are never close/minimize targets (spec §281-285)."""
    from app.desktop.window_manager import is_own_process

    if window.pid and is_own_process(window.pid):
        return True
    title = (window.title or "").casefold()
    process_name = (window.process_name or "").casefold()
    protected_markers = ("nyra-desktop", "nyra backend", "nyra presence", "tauri")
    return any(marker in title or marker in process_name for marker in protected_markers)


def _window_relevant(window: WindowInfo, candidate: ApplicationCandidate) -> bool:
    """Match a window to a discovery candidate by process name or display tokens."""
    process_name = (window.process_name or "").casefold().removesuffix(".exe")
    target_stem = Path(expand_launch_target(candidate.target)).stem.casefold() if candidate.launch_method == LaunchMethod.EXE else ""
    if target_stem and process_name == target_stem:
        return True
    title = (window.title or "").casefold()
    if not title:
        return False
    tokens = [token for token in re_split_tokens(candidate.display_name) if len(token) >= 3]
    return any(token in title for token in tokens)


def re_split_tokens(value: str) -> list[str]:
    return re.split(r"[\s\-_.]+", value.casefold())


def _shell_execute(candidate: ApplicationCandidate) -> bool:
    """Open via ShellExecuteW: .lnk, shell:AUMID, URI or file association."""
    try:
        if candidate.launch_method == LaunchMethod.APP_USER_MODEL_ID:
            target = candidate.target if candidate.target.startswith("shell:") else f"shell:AppsFolder\\{candidate.target}"
        else:
            target = expand_launch_target(candidate.target)
        result = ctypes.windll.shell32.ShellExecuteW(None, "open", target, None, None, 1)
        return int(result) > 32
    except (AttributeError, OSError):
        try:
            os.startfile(expand_launch_target(candidate.target))  # type: ignore[attr-defined]  # noqa: S606
            return True
        except OSError:
            return False


class DesktopController:
    def __init__(
        self,
        event_bus: EventBus,
        apps_path: Path | None = None,
        *,
        dynamic_discovery: bool = True,
    ) -> None:
        self.event_bus = event_bus
        self.apps_path = Path(apps_path) if apps_path else CONFIG_ROOT / "desktop_apps.yaml"
        self.registry = DesktopAppsRegistry()
        self._launched_pids: dict[str, set[int]] = {}
        self.discovery = ApplicationDiscovery(enabled=dynamic_discovery)

    async def initialize(self) -> None:
        self.registry = load_desktop_apps(self.apps_path)

    # ------------------------------------------------------------------ query

    def spec(self, app_id: str) -> DesktopAppSpec | None:
        return self.registry.get(app_id)

    def resolve_registered_app_id(self, value: str) -> str | None:
        """Resolve an id or human display name to one trusted registry entry."""
        if self.spec(value) is not None:
            return value
        needle = normalize(value)
        matches = [
            spec.id for spec in self.registry.valid_specs()
            if needle and needle in {normalize(spec.id), normalize(spec.display_name)}
        ]
        return matches[0] if len(matches) == 1 else None

    def list_apps(self) -> dict:
        apps = []
        for entry in self.registry.entries:
            item: dict = {"id": entry.app_id}
            if entry.spec is not None:
                spec = entry.spec
                windows = self.windows_for(spec)
                item.update({
                    "display_name": spec.display_name,
                    "enabled": spec.enabled,
                    "executable": spec.executable,
                    "single_instance": spec.single_instance,
                    "startup_timeout_seconds": spec.startup_timeout_seconds,
                    "risk": spec.risk,
                    "open_windows": [
                        {"pid": window.pid, "hwnd": window.hwnd, "title": window.title}
                        for window in windows
                    ],
                })
            else:
                item.update({"enabled": False, "validation_error": entry.error})
            apps.append(item)
        return {"success": True, "apps": apps}

    def windows_for(self, spec: DesktopAppSpec) -> list[WindowInfo]:
        tracked = self._launched_pids.get(spec.id, set())
        return find_windows_for_app(
            process_names=spec.normalized_process_names(),
            title_contains=spec.window_title_contains,
            extra_pids=set(tracked),
        )

    def status_windows(self, app_id: str | None = None, query: str | None = None) -> dict:
        if app_id:
            spec = self.spec(app_id)
            if spec is None:
                error = self.registry.error_for(app_id)
                return {
                    "success": False, "app": app_id,
                    "error_code": LaunchErrorCode.INVALID_CONFIGURATION.value if error else LaunchErrorCode.UNKNOWN_APP.value,
                    "message": error or "Aplicativo não registrado no Desktop Apps Registry.",
                }
            windows = self.windows_for(spec)
            return {
                "success": True,
                "verification_status": "VERIFIED",
                "app": app_id,
                "open": bool(windows),
                "windows": [window.model_dump(mode="json") for window in windows],
            }
        if query:
            normalized = query.strip()
            if len(normalized) < 2:
                return {"success": False, "query": normalized, "error_code": "INVALID_QUERY", "message": "Consulta muito curta.", "windows": []}
            needle = normalized.casefold()
            tokens = [token for token in needle.replace(",", " ").split() if len(token) >= 3]
            windows = annotate_process_names(list_visible_windows())
            relevant = [
                window.model_dump(mode="json") for window in windows
                if needle in (window.title or "").casefold()
                or any(token in (window.title or "").casefold() for token in tokens)
                or needle in (window.process_name or "").casefold().removesuffix(".exe")
            ]
            return {
                "success": True,
                "verification_status": "VERIFIED" if relevant else "NOT_REQUIRED",
                "query": normalized,
                "open": bool(relevant),
                "windows": relevant[:10],
            }
        windows = annotate_process_names(list_visible_windows())
        registered_names = {
            name.casefold()
            for spec in self.registry.valid_specs()
            for name in spec.normalized_process_names()
        }
        relevant = [
            window.model_dump(mode="json") for window in windows
            if (window.process_name or "").casefold() in registered_names
        ]
        return {"success": True, "verification_status": "VERIFIED", "windows": relevant}

    # ------------------------------------------------- dynamic discovery

    def find(self, query: str, limit: int = 8) -> dict:
        started = time.perf_counter()
        result = self.discovery.resolve(query.strip()) if limit >= 1 else {"status": "INVALID", "candidates": []}
        candidates = result.get("candidates", [])
        payload = {
            "success": result["status"] not in {"DISABLED", "INVALID"},
            "query": query.strip(),
            "status": result["status"],
            "candidates": candidates,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        logger.info(
            "desktop_find_application",
            extra={"query": redact_query(query), "status": result["status"], "matches": len(candidates)},
        )
        return payload

    async def launch_dynamic(self, query: str, *, origin: str = "operator") -> dict:
        """Universal launch: free-text app request resolved by discovery (#56..#67)."""
        started = time.perf_counter()

        def done(**kwargs) -> dict:
            payload = operation_result(app=query.strip(), action="launch_dynamic", duration_ms=(time.perf_counter() - started) * 1000, **kwargs)
            logger.info(
                "desktop_operation",
                extra={
                    "app": redact_query(query), "action": "launch_dynamic", "origin": origin,
                    "success": bool(payload.get("success")),
                    "effect_verified": payload.get("effect_verified"),
                    "duration_ms": payload.get("duration_ms"),
                },
            )
            return payload

        clean = query.strip()
        resolution = self.discovery.resolve(clean)
        status = resolution.get("status")
        if status == "DISABLED":
            return done(success=False, error_code=LaunchErrorCode.UNKNOWN_APP.value,
                        message="Descoberta dinâmica desabilitada; use um id do Desktop Apps Registry.")
        if status in {"EXACT_MATCH", "HIGH_CONFIDENCE"}:
            candidate_data = resolution.get("candidate") or {}
            candidate = ApplicationCandidate(
                id=candidate_data.get("id", ""),
                display_name=candidate_data.get("display_name", ""),
                source=candidate_data.get("source", ""),
                launch_method=candidate_data.get("launch_method", ""),
                target=candidate_data.get("target", ""),
                confidence=float(candidate_data.get("confidence", 0.0)),
            )
            expected_window = candidate.display_name.casefold() not in _BACKGROUND_HINTS
            return await self._launch_candidate(candidate, done, origin=origin, expected_window=expected_window)
        if status == "AMBIGUOUS":
            options = resolution.get("candidates", [])
            names = ", ".join(f"{item['display_name']} ({item['launch_method']})" for item in options)
            return done(
                success=False,
                error_code="AMBIGUOUS_APPLICATION",
                message=f"Há mais de um aplicativo plausível para '{redact_query(clean)}': {names}. Pergunte ao operador qual abrir antes de executar.",
                detail={"options": options},
            )
        return done(
            success=False,
            error_code=LaunchErrorCode.EXECUTABLE_NOT_FOUND.value,
            message=f"Nenhuma aplicação instalada correspondente a '{redact_query(clean)}' foi encontrada nas fontes seguras de descoberta.",
        )

    async def _launch_candidate(self, candidate: ApplicationCandidate, done, *, origin: str, expected_window: bool = True) -> dict:
        method = candidate.launch_method
        pre_existing = {
            window.hwnd for window in annotate_process_names(list_visible_windows())
            if _window_relevant(window, candidate)
        }
        pid = None
        try:
            if method == LaunchMethod.EXE:
                target = expand_launch_target(candidate.target)
                executable = shutil.which(target) if ("\\" not in target and "/" not in target) else target
                if not executable or not Path(executable).is_file():
                    return done(success=False, error_code=LaunchErrorCode.EXECUTABLE_NOT_FOUND.value,
                                message=f"Executável '{candidate.target}' não encontrado.", execution_success=False,
                                effect_verified=False, verification_status="EXECUTION_FAILED",
                                detail={"candidate": candidate.public_dict()})
                creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                process = subprocess.Popen(  # noqa: S603 - alvo resolvido por descoberta validada
                    [executable], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, close_fds=True, creationflags=creationflags,
                )
                pid = process.pid
                poll_process = process.poll
                deadline = time.monotonic() + 15.0
                min_age = 1.6
                first_seen: dict[int, float] = {}
                confirmed: list[WindowInfo] = []
                allow_pid_free = False
                while time.monotonic() < deadline:
                    await asyncio.sleep(0.35)
                    children = set()
                    try:
                        children = {child.pid for child in process.children()}
                    except Exception:  # noqa: BLE001
                        pass
                    # UWP/stub: o launcher pode sair e a janela real nasce em um
                    # ApplicationFrameHost fora da linhagem; nesse caso janelas
                    # NOVAS por título continuam evidência legítima.
                    if allow_pid_free:
                        found = [
                            window for window in annotate_process_names(list_visible_windows())
                            if _window_relevant(window, candidate)
                            and window.hwnd not in pre_existing
                        ]
                    else:
                        found = [
                            window for window in annotate_process_names(list_visible_windows())
                            if _window_relevant(window, candidate)
                            and window.pid not in pre_existing
                            and (window.pid in ({process.pid} | children))
                        ]
                    now = time.monotonic()
                    for window in found:
                        first_seen.setdefault(window.hwnd, now)
                    confirmed = [
                        window for window in found
                        if (now - first_seen.get(window.hwnd, now)) >= min_age
                    ]
                    if confirmed:
                        break
                    if poll_process() is not None and not children and not found:
                        if allow_pid_free:
                            continue
                        allow_pid_free = True
                        continue
                if not expected_window:
                    alive = poll_process() is None
                    return done(success=alive, message="Processo em background iniciado; verificação por janela não aplicável.",
                                execution_success=True, effect_verified=alive, verification_status="VERIFIED" if alive else "VERIFICATION_FAILED",
                                detail={"candidate": candidate.public_dict(), "pid": process.pid})
                if not confirmed:
                    terminated = self._terminate_own_process(process)
                    return done(success=False, error_code=LaunchErrorCode.WINDOW_NOT_CONFIRMED.value,
                                message="Processo iniciado, mas nenhuma janela visível foi confirmada dentro do timeout."
                                        + (" Processo encerrado por ser da própria NYRA." if terminated else ""),
                                execution_success=True, effect_verified=False, verification_status="VERIFICATION_FAILED",
                                detail={"candidate": candidate.public_dict(), "pid": process.pid})
                await self._publish_verified(candidate, confirmed[0].pid)
                return done(success=True,
                            message=f"'{candidate.display_name}' aberto via {method}; janela visível confirmada (pid {confirmed[0].pid}).",
                            execution_success=True, effect_verified=True, verification_status="VERIFIED",
                            detail={"candidate": candidate.public_dict(), "pid": process.pid,
                                    "windows": [window.model_dump(mode="json") for window in confirmed[:5]]})
            # SHELL_EXECUTE / START_MENU / APP_USER_MODEL_ID / URI / FILE_ASSOCIATION
            launched = await asyncio.to_thread(_shell_execute, candidate)
            if not launched:
                return done(success=False, error_code=LaunchErrorCode.SPAWN_FAILED.value,
                            message=f"Falha ao acionar '{candidate.display_name}' via {method}.",
                            execution_success=False, effect_verified=False, verification_status="EXECUTION_FAILED",
                            detail={"candidate": candidate.public_dict()})
            deadline = time.monotonic() + 15.0
            confirmed: list[WindowInfo] = []
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                confirmed = [
                    window for window in annotate_process_names(list_visible_windows())
                    if _window_relevant(window, candidate) and window.hwnd not in pre_existing
                ]
                if confirmed:
                    break
            if not expected_window:
                return done(success=True, message="Comando de abertura executado; verificação por janela não aplicável.",
                            execution_success=True, effect_verified=None, verification_status="EXECUTED",
                            detail={"candidate": candidate.public_dict()})
            if confirmed:
                await self._publish_verified(candidate, confirmed[0].pid)
                return done(success=True,
                            message=f"'{candidate.display_name}' aberto via {method}; janela visível confirmada (pid {confirmed[0].pid}).",
                            execution_success=True, effect_verified=True, verification_status="VERIFIED",
                            detail={"candidate": candidate.public_dict(),
                                    "windows": [window.model_dump(mode="json") for window in confirmed[:5]]})
            return done(success=False, error_code=LaunchErrorCode.WINDOW_NOT_CONFIRMED.value,
                        message=f"'{candidate.display_name}' foi acionado, mas nenhuma janela nova foi confirmada no desktop.",
                        execution_success=True, effect_verified=False, verification_status="VERIFICATION_FAILED",
                        detail={"candidate": candidate.public_dict()})
        except OSError as exc:
            return done(success=False, error_code=LaunchErrorCode.SPAWN_FAILED.value,
                        message=f"spawn falhou: {type(exc).__name__}: {str(exc)[:120]}",
                        execution_success=False, effect_verified=False, verification_status="EXECUTION_FAILED",
                        detail={"candidate": candidate.public_dict()})

    async def _publish_verified(self, candidate: ApplicationCandidate, pid: int) -> None:
        await self.event_bus.publish(
            EventType.DESKTOP_WINDOW_VERIFIED,
            app=candidate.id, display_name=candidate.display_name,
            pid=pid, turn_id=get_current_turn_id(),
        )

    # ------------------------------------------------------- window operations

    def _resolve_windows(self, *, app: str = "", query: str = "", hwnd: int | None = None) -> list[WindowInfo]:
        if hwnd:
            from app.desktop.window_manager import window_still_alive

            if not window_still_alive(hwnd):
                return []
            windows = [window for window in annotate_process_names(list_visible_windows()) if window.hwnd == hwnd]
            return windows
        if app:
            spec = self.spec(app)
            if spec is not None:
                return self.windows_for(spec)
        needle = (query or "").strip().casefold()
        if not needle:
            return []
        tokens = [token for token in re.split(r"[\s\-_,]+", needle) if len(token) >= 3]
        matches: list[WindowInfo] = []
        for window in annotate_process_names(list_visible_windows()):
            if window.window_class == "#32770":
                continue  # diálogos modais só são alvo via hwnd explícito
            title = (window.title or "").casefold()
            process_name = (window.process_name or "").casefold().removesuffix(".exe")
            if needle in title or needle == process_name or any(token in title for token in tokens):
                matches.append(window)
        return matches

    async def _window_operation(
        self,
        action: str,
        *,
        app: str = "",
        query: str = "",
        hwnd: int | None = None,
        verify_and_mutate=None,
        success_detail=None,
    ) -> dict:
        """Shared ACT→VERIFY pipeline for all window mutations."""
        started = time.perf_counter()

        def done(**kwargs) -> dict:
            payload = operation_result(app=app or query[:40], action=action, duration_ms=(time.perf_counter() - started) * 1000, **kwargs)
            logger.info(
                "desktop_operation",
                extra={
                    "app": redact_query(app or query), "action": action,
                    "success": bool(payload.get("success")),
                    "effect_verified": payload.get("effect_verified"),
                    "duration_ms": payload.get("duration_ms"),
                },
            )
            return payload

        targets = self._resolve_windows(app=app, query=query, hwnd=hwnd)
        targets = [
            window for window in targets
            if not _is_protected_window(window)
        ]
        if not targets:
            return done(
                success=False,
                error_code="WINDOW_NOT_FOUND" if (app or query or hwnd) else "TARGET_REQUIRED",
                message=(
                    "Nenhuma janela visível correspondente foi encontrada agora."
                    if (app or query or hwnd)
                    else "Informe app, query ou hwnd alvo."
                ),
                execution_success=False, effect_verified=False,
                verification_status="NOT_EXECUTED",
            )
        results: list[dict] = []
        verified_any = False
        for window in targets[:5]:
            try:
                ok = await asyncio.to_thread(verify_and_mutate, window.hwnd)
            except Exception as exc:  # noqa: BLE001
                ok = False
                logger.info("desktop_window_operation_failed", extra={"action": action, "error_type": type(exc).__name__})
            results.append({"hwnd": window.hwnd, "pid": window.pid, "title": window.title[:80], "verified": bool(ok)})
            verified_any = verified_any or bool(ok)
        detail = {"windows": results}
        if success_detail:
            detail.update(success_detail())
        return done(
            success=verified_any,
            error_code=None if verified_any else f"{action.upper()}_NOT_CONFIRMED",
            message=(
                f"{action} verificado em {sum(1 for item in results if item['verified'])}/{len(results)} janela(s)."
                if verified_any else
                f"Ação '{action}' não pôde ser confirmada em nenhuma janela alvo."
            ),
            execution_success=True,
            effect_verified=verified_any,
            verification_status="VERIFIED" if verified_any else "VERIFICATION_FAILED",
            detail=detail,
        )

    async def focus(self, *, app: str = "", query: str = "", hwnd: int | None = None) -> dict:
        from app.desktop import window_manager

        return await self._window_operation(
            "focus", app=app, query=query, hwnd=hwnd,
            verify_and_mutate=window_manager.focus_window,
            success_detail=lambda: {"foreground_expected": True},
        )

    async def minimize(self, *, app: str = "", query: str = "", hwnd: int | None = None) -> dict:
        from app.desktop import window_manager

        return await self._window_operation(
            "minimize", app=app, query=query, hwnd=hwnd,
            verify_and_mutate=window_manager.minimize_window,
        )

    async def maximize(self, *, app: str = "", query: str = "", hwnd: int | None = None) -> dict:
        from app.desktop import window_manager

        return await self._window_operation(
            "maximize", app=app, query=query, hwnd=hwnd,
            verify_and_mutate=window_manager.maximize_window,
        )

    async def restore(self, *, app: str = "", query: str = "", hwnd: int | None = None) -> dict:
        from app.desktop import window_manager

        return await self._window_operation(
            "restore", app=app, query=query, hwnd=hwnd,
            verify_and_mutate=window_manager.restore_window,
        )

    async def move_window(self, x: int, y: int, *, app: str = "", query: str = "", hwnd: int | None = None) -> dict:
        from app.desktop import window_manager

        return await self._window_operation(
            "move", app=app, query=query, hwnd=hwnd,
            verify_and_mutate=lambda handle: window_manager.move_window(handle, x, y),
            success_detail=lambda: {"x": x, "y": y},
        )

    async def resize_window(self, width: int, height: int, *, app: str = "", query: str = "", hwnd: int | None = None) -> dict:
        from app.desktop import window_manager

        return await self._window_operation(
            "resize", app=app, query=query, hwnd=hwnd,
            verify_and_mutate=lambda handle: window_manager.resize_window(handle, width, height),
            success_detail=lambda: {"width": width, "height": height},
        )

    async def close(self, *, app: str = "", query: str = "", hwnd: int | None = None) -> dict:
        """Graceful WM_CLOSE with per-window verification (§26, §39-41)."""
        from app.desktop import window_manager

        return await self._window_operation(
            "close", app=app, query=query, hwnd=hwnd,
            verify_and_mutate=window_manager.graceful_close,
        )

    async def open_file(self, path: str, *, app: str = "") -> dict:
        started = time.perf_counter()

        def done(**kwargs) -> dict:
            return operation_result(app=app or "file_association", action="open_file", duration_ms=(time.perf_counter() - started) * 1000, **kwargs)

        clean = path.strip().strip('"')
        candidate = Path(clean).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        resolved = candidate.resolve()
        verb = "open" if not app else "open"
        if not resolved.exists():
            return done(success=False, error_code="FILE_NOT_FOUND",
                        message=f"Arquivo inexistente: {resolved}", execution_success=False, effect_verified=False)
        if app and self.spec(app) is None:
            return done(success=False, error_code=LaunchErrorCode.UNKNOWN_APP.value,
                        message=f"Aplicativo '{app}' não registrado.", execution_success=False)
        try:
            if app:
                executable = shutil.which(self.spec(app).executable) or self.spec(app).executable
                creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                subprocess.Popen(  # noqa: S603 - executável vem do registry confiável; caminho validado acima
                    [executable, str(resolved)], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=True, creationflags=creationflags,
                )
            else:
                result = ctypes.windll.shell32.ShellExecuteW(None, verb, str(resolved), None, None, 1)
                if int(result) <= 32:
                    return done(success=False, error_code="OPEN_FAILED",
                                message=f"ShellExecute retornou {result}.", execution_success=False)
        except OSError as exc:
            return done(success=False, error_code="OPEN_FAILED",
                        message=f"{type(exc).__name__}: {str(exc)[:120]}", execution_success=False)
        return done(success=True, message=f"Solicitação de abertura enviada para {resolved.name}.",
                    execution_success=True, effect_verified=True, verification_status="EXECUTED",
                    detail={"path": str(resolved), "with_app": app or None})

    async def open_url(self, url: str) -> dict:
        started = time.perf_counter()

        def done(**kwargs) -> dict:
            return operation_result(app="browser", action="open_url", duration_ms=(time.perf_counter() - started) * 1000, **kwargs)

        clean = url.strip()
        allowed_schemes = ("http://", "https://", "ms-settings:", "shell:", "file://")
        if not clean.casefold().startswith(allowed_schemes):
            return done(success=False, error_code="INVALID_URL",
                        message="Somente http/https/ms-settings/shell/file são aceitos.",
                        execution_success=False)
        try:
            result = ctypes.windll.shell32.ShellExecuteW(None, "open", clean, None, None, 1)
        except OSError as exc:
            return done(success=False, error_code="OPEN_FAILED",
                        message=f"{type(exc).__name__}: {str(exc)[:120]}", execution_success=False)
        if int(result) <= 32:
            return done(success=False, error_code="OPEN_FAILED",
                        message=f"ShellExecute retornou {result}.", execution_success=False)
        return done(success=True, message=f"Navegação solicitada para {clean[:80]}.",
                    execution_success=True, effect_verified=True, verification_status="EXECUTED",
                    detail={"url": clean})

    # ------------------------------------------------------------- ui automation

    async def _uia_call(self, fn, *args, **kwargs) -> dict:
        """Run a blocking UIA function off the event loop with tool-shaped errors."""
        started = time.perf_counter()
        try:
            result = await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", "UI_ACTION_FAILED")
            message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
            logger.info("desktop_uia_failed", extra={"code": str(code), "error_type": type(exc).__name__})
            return operation_result(
                app="ui", action="uia", duration_ms=(time.perf_counter() - started) * 1000,
                success=False, error_code=str(code), message=message,
                execution_success=False, effect_verified=False,
                verification_status="EXECUTION_FAILED",
            )
        payload = operation_result(
            app="ui", action="uia", duration_ms=(time.perf_counter() - started) * 1000,
            success=True,
        )
        payload.update(result)
        if "effect_verified" not in payload:
            payload["effect_verified"] = True
        logger.info("desktop_operation", extra={
            "app": "ui", "action": payload.get("method") or payload.get("action"),
            "success": True, "effect_verified": payload.get("effect_verified"),
            "duration_ms": payload.get("duration_ms"),
        })
        return payload

    def _window_hwnd_for_uia(self, *, app: str = "", query: str = "", hwnd: int | None = None) -> int:
        targets = self._resolve_windows(app=app, query=query, hwnd=hwnd)
        if not targets:
            raise ValueError("WINDOW_NOT_FOUND:Nenhuma janela visível correspondente foi encontrada.")
        return targets[0].hwnd

    async def ui_inspect(self, *, app: str = "", query: str = "", hwnd: int | None = None, max_depth: int = 5) -> dict:
        from app.desktop import uia

        try:
            handle = self._window_hwnd_for_uia(app=app, query=query, hwnd=hwnd)
        except ValueError as exc:
            code, message = str(exc).split(":", 1)
            return operation_result(app="ui", action="inspect", success=False, error_code=code, message=message,
                                    execution_success=False, effect_verified=False)
        return await self._uia_call(uia.inspect_window, handle, max_depth=max_depth)

    async def ui_find(self, *, name: str = "", automation_id: str = "", control_type: str = "", class_name: str = "",
                      app: str = "", query: str = "", hwnd: int | None = None) -> dict:
        from app.desktop import uia

        try:
            handle = self._window_hwnd_for_uia(app=app, query=query, hwnd=hwnd)
        except ValueError as exc:
            code, message = str(exc).split(":", 1)
            return operation_result(app="ui", action="find", success=False, error_code=code, message=message,
                                    execution_success=False, effect_verified=False)
        return await self._uia_call(
            uia.find_in_window, handle, name=name, automation_id=automation_id,
            control_type=control_type, class_name=class_name,
        )

    async def ui_click(self, *, name: str = "", automation_id: str = "", control_type: str = "",
                       app: str = "", query: str = "", hwnd: int | None = None) -> dict:
        from app.desktop import uia

        try:
            handle = self._window_hwnd_for_uia(app=app, query=query, hwnd=hwnd)
        except ValueError as exc:
            code, message = str(exc).split(":", 1)
            return operation_result(app="ui", action="click", success=False, error_code=code, message=message,
                                    execution_success=False, effect_verified=False)
        return await self._uia_call(
            uia.click_element, handle, name=name, automation_id=automation_id, control_type=control_type,
        )

    async def ui_set_text(self, value: str, *, name: str = "", automation_id: str = "", control_type: str = "",
                          app: str = "", query: str = "", hwnd: int | None = None) -> dict:
        from app.desktop import uia

        try:
            handle = self._window_hwnd_for_uia(app=app, query=query, hwnd=hwnd)
        except ValueError as exc:
            code, message = str(exc).split(":", 1)
            return operation_result(app="ui", action="set_text", success=False, error_code=code, message=message,
                                    execution_success=False, effect_verified=False)
        return await self._uia_call(
            uia.set_text, handle, value, name=name, automation_id=automation_id, control_type=control_type,
        )

    async def ui_get_text(self, *, name: str = "", automation_id: str = "", control_type: str = "",
                          app: str = "", query: str = "", hwnd: int | None = None) -> dict:
        from app.desktop import uia

        try:
            handle = self._window_hwnd_for_uia(app=app, query=query, hwnd=hwnd)
        except ValueError as exc:
            code, message = str(exc).split(":", 1)
            return operation_result(app="ui", action="get_text", success=False, error_code=code, message=message,
                                    execution_success=False, effect_verified=False)
        return await self._uia_call(
            uia.get_text, handle, name=name, automation_id=automation_id, control_type=control_type,
        )

    async def ui_send_keys(self, text: str, *, app: str = "", query: str = "", hwnd: int | None = None) -> dict:
        from app.desktop import uia
        from app.desktop.window_manager import focus_window

        try:
            handle = self._window_hwnd_for_uia(app=app, query=query, hwnd=hwnd)
        except ValueError as exc:
            code, message = str(exc).split(":", 1)
            return operation_result(app="ui", action="send_keys", success=False, error_code=code, message=message,
                                    execution_success=False, effect_verified=False)
        focused = await asyncio.to_thread(focus_window, handle)
        if not focused:
            return operation_result(app="ui", action="send_keys", success=False, error_code="FOCUS_NOT_CONFIRMED",
                                    message="Não consegui trazer a janela alvo para o primeiro plano; teclado abortado.",
                                    execution_success=False, effect_verified=False)
        return await self._uia_call(uia.send_keys_to_foreground, text, handle)

    # ----------------------------------------------------------------- launch

    def _discard_launch_tracking(self, app_id: str, pid: int) -> None:
        tracked = self._launched_pids.get(app_id)
        if tracked is not None:
            tracked.discard(pid)

    @staticmethod
    def _terminate_own_process(process: subprocess.Popen) -> bool:
        """Encerra processo filho da própria NYRA quando a verificação de janela falha."""
        try:
            process.terminate()
            process.wait(timeout=3)
            return True
        except Exception:  # noqa: BLE001
            try:
                process.kill()
                return True
            except Exception:  # noqa: BLE001
                return False

    async def launch(self, app_id: str, *, origin: str = "operator") -> dict:
        started = time.perf_counter()
        app_id = self.resolve_registered_app_id(app_id) or app_id

        def done(**kwargs) -> dict:
            payload = operation_result(app=app_id, action="launch", duration_ms=(time.perf_counter() - started) * 1000, **kwargs)
            logger.info(
                "desktop_operation",
                extra={
                    "app": app_id, "action": "launch", "origin": origin,
                    "success": bool(payload.get("success")),
                    "effect_verified": payload.get("effect_verified"),
                    "duration_ms": payload.get("duration_ms"),
                },
            )
            return payload

        spec = self.spec(app_id)
        if spec is None:
            error = self.registry.error_for(app_id)
            return done(
                success=False, error_code=(
                    LaunchErrorCode.INVALID_CONFIGURATION.value if error else LaunchErrorCode.UNKNOWN_APP.value
                ),
                message=error or "Aplicativo não registrado no Desktop Apps Registry.",
            )
        if not spec.enabled:
            return done(success=False, error_code=LaunchErrorCode.INVALID_CONFIGURATION.value, message="Aplicativo desabilitado no registry.")

        existing = self.windows_for(spec)
        if existing and spec.single_instance:
            await self.event_bus.publish(
                EventType.DESKTOP_WINDOW_VERIFIED,
                app=app_id,
                pid=existing[0].pid,
                turn_id=get_current_turn_id(),
            )
            return done(
                success=True,
                message="already_open; janela visível confirmada sem nova instância.",
                execution_success=True, effect_verified=True, verification_status="VERIFIED",
                detail={"already_open": True, "windows": [window.model_dump(mode="json") for window in existing[:3]]},
            )

        # Snapshot PRÉ-launch: janelas já visíveis pertencem ao operador e NUNCA
        # podem ser usadas para confirmar ESTA abertura (honestidade da verificação).
        pre_existing_hwnds = {window.hwnd for window in existing}

        executable = shutil.which(spec.executable) if ("\\" not in spec.executable and "/" not in spec.executable) else spec.executable
        if not executable or not Path(executable).is_file():
            return done(
                success=False, error_code=LaunchErrorCode.EXECUTABLE_NOT_FOUND.value,
                message=f"Executável '{spec.executable}' não foi encontrado neste host.",
            )

        argv = [executable, *spec.arguments]
        creationflags = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            process = subprocess.Popen(  # noqa: S603 - argv vem exclusivamente do registry confiável
                argv,
                cwd=str(Path(spec.working_directory)) if spec.working_directory else None,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, creationflags=creationflags,
            )
        except OSError as exc:
            return done(
                success=False, error_code=LaunchErrorCode.SPAWN_FAILED.value,
                message=f"spawn falhou: {type(exc).__name__}: {str(exc)[:120]}",
                execution_success=False, effect_verified=False, verification_status="EXECUTION_FAILED",
            )

        self._launched_pids.setdefault(app_id, set()).add(process.pid)
        await self.event_bus.publish(EventType.DESKTOP_APP_LAUNCHED, app=app_id, pid=process.pid, turn_id=get_current_turn_id())

        deadline = time.monotonic() + float(spec.startup_timeout_seconds)
        confirmed_windows: list[WindowInfo] = []
        first_seen: dict[int, float] = {}
        min_visible_age_seconds = 1.6
        while True:
            await asyncio.sleep(0.35)
            tracked = {process.pid} | self._launched_pids.get(app_id, set())
            children: set[int] = set()
            try:
                children = {child.pid for child in process.children()}
            except Exception:  # noqa: BLE001 - psutil pode falhar se o processo já saiu
                children = set()
            candidates = find_windows_for_app(
                process_names=spec.normalized_process_names(),
                title_contains=spec.window_title_contains,
                extra_pids={*tracked, *children},
            )
            # Somente janelas NOVAS (não presentes antes do spawn) ou do nosso PID
            # comprovam ESTA abertura. Flash transitório de console durante o boot do
            # processo é descartado exigindo persistência mínima na amostragem.
            current_now = time.monotonic()
            current_hwnds = set()
            for window in candidates:
                if window.hwnd in pre_existing_hwnds and window.pid not in tracked and window.pid not in children:
                    continue
                current_hwnds.add(window.hwnd)
                first_seen.setdefault(window.hwnd, current_now)
            confirmed_windows = [
                window for window in candidates
                if window.hwnd in current_hwnds
                and (current_now - first_seen.get(window.hwnd, current_now)) >= min_visible_age_seconds
            ]
            # Limpa janelas que sumiram antes de atingir a idade mínima.
            for hwnd in list(first_seen):
                if hwnd not in current_hwnds:
                    first_seen.pop(hwnd, None)
            if confirmed_windows:
                break
            if process.poll() is not None and not children:
                self._discard_launch_tracking(app_id, process.pid)
                return done(
                    success=False, error_code=LaunchErrorCode.WINDOW_NOT_CONFIRMED.value,
                    message=f"O processo {process.pid} encerrou antes de criar janela; consulte o executável/argumentos.",
                    execution_success=True, effect_verified=False, verification_status="VERIFICATION_FAILED",
                    detail={"pid": process.pid},
                )
            if time.monotonic() >= deadline:
                terminated = self._terminate_own_process(process)
                return done(
                    success=False, error_code=LaunchErrorCode.WINDOW_NOT_CONFIRMED.value,
                    message="Processo iniciado, mas nenhuma janela visível correspondente foi confirmada no desktop dentro do timeout."
                            + (" Processo encerrado pela NYRA por ser de sua própria autoria." if terminated else ""),
                    execution_success=True, effect_verified=False, verification_status="VERIFICATION_FAILED",
                    detail={"pid": process.pid, "timeout_seconds": spec.startup_timeout_seconds,
                            "cleaned_up": terminated},
                )

        window_payloads = [window.model_dump(mode="json") for window in confirmed_windows[:5]]
        await self.event_bus.publish(
            EventType.DESKTOP_WINDOW_VERIFIED,
            app=app_id,
            pid=confirmed_windows[0].pid,
            turn_id=get_current_turn_id(),
        )
        return done(
            success=True,
            message=f"Janela visível confirmada no desktop ({len(confirmed_windows)} janela(s), pid {confirmed_windows[0].pid}).",
            execution_success=True, effect_verified=True, verification_status="VERIFIED",
            detail={"pid": process.pid, "windows": window_payloads},
        )
