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
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from app.core.paths import CONFIG_ROOT, DATA_ROOT, PROJECT_ROOT
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
from app.desktop.windows import (
    annotate_process_names,
    find_windows_for_app,
    list_application_windows,
    list_visible_windows,
)
from app.events import EventBus, EventType

logger = logging.getLogger("nyra.desktop")

_BACKGROUND_HINTS = {"steam", "discord", "onedrive", "dropbox"}

# ShellExecute dispatches by file association and can start executables,
# scripts and shortcuts. Only inert document/media types may use the default
# association; every other target must name a trusted registry application or
# go through system_shell.
_SAFE_ASSOCIATION_SUFFIXES = {
    ".bmp", ".cfg", ".conf", ".csv", ".doc", ".docx", ".flac", ".gif",
    ".htm", ".html", ".ini", ".jpeg", ".jpg", ".json", ".log", ".md",
    ".mkv", ".mov", ".mp3", ".mp4", ".odp", ".ods", ".odt", ".ogg",
    ".pdf", ".png", ".ppt", ".pptx", ".rtf", ".svg", ".toml", ".tsv",
    ".txt", ".wav", ".webm", ".webp", ".xls", ".xlsx", ".xml", ".yaml",
    ".yml",
}


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
    expected_processes = {
        name.casefold().removesuffix(".exe")
        for name in candidate.process_names
        if name
    }
    if process_name and process_name in expected_processes:
        return True
    target_stem = Path(expand_launch_target(candidate.target)).stem.casefold() if candidate.launch_method == LaunchMethod.EXE else ""
    if target_stem and process_name == target_stem:
        return True
    title = (window.title or "").casefold()
    if not title:
        return False
    # comparação sem acento: "Configurações" casa com "configuracoes"
    norm_title = normalize(title)
    tokens = [normalize(token) for token in re_split_tokens(candidate.display_name)]
    return any(len(token) >= 3 and token in norm_title for token in tokens)


def _matching_process_pids(candidate: ApplicationCandidate) -> set[int]:
    """Snapshot real process identities advertised by the Application Registry."""
    names = {
        name.casefold().removesuffix(".exe")
        for name in candidate.process_names
        if name
    }
    if not names:
        return set()
    matches: set[int] = set()
    for process in psutil.process_iter(["pid", "name"]):
        try:
            name = str(process.info.get("name") or "").casefold().removesuffix(".exe")
            if name in names:
                matches.add(int(process.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, TypeError):
            continue
    return matches


def _is_console_exe(path: str) -> bool:
    """True para executáveis de console (subsystem 3): Popen DETACHED não cria janela."""
    try:
        with open(path, "rb") as handle:
            dos = handle.read(64)
            if len(dos) < 64 or dos[:2] != b"MZ":
                return False
            e_lfanew = int.from_bytes(dos[0x3C:0x40], "little")
            handle.seek(e_lfanew)
            if handle.read(4) != b"PE\x00\x00":
                return False
            # optional header começa em e_lfanew + 24; subsystem no offset 68
            handle.seek(e_lfanew + 24 + 68)
            subsystem = int.from_bytes(handle.read(2), "little")
            return subsystem == 3
    except OSError:
        return False


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
        universal=None,
        approvals=None,
    ) -> None:
        self.event_bus = event_bus
        self.apps_path = Path(apps_path) if apps_path else CONFIG_ROOT / "desktop_apps.yaml"
        self.registry = DesktopAppsRegistry()
        self._launched_pids: dict[str, set[int]] = {}
        self.discovery = ApplicationDiscovery(enabled=dynamic_discovery)
        # Universal Application Registry (nyra-full §2/§3): índice persistente +
        # aprendizado de aliases verificados.
        from app.desktop.universal_registry import UniversalAppRegistry

        self.universal = universal or UniversalAppRegistry(discovery=self.discovery)
        self.approvals = approvals
        self.last_controlled: dict[str, str] | None = None
        self.last_operation_result: dict | None = None
        self._universal_dedup: dict[tuple, str] = {}

    def _note_controlled(
        self,
        display_name: str,
        *,
        kind: str = "app",
        process_names: list[str] | tuple[str, ...] | None = None,
        title_tokens: list[str] | tuple[str, ...] | None = None,
        path: str | None = None,
        hwnd: int | None = None,
        canonical_id: str = "",
    ) -> None:
        """Contexto para referências como 'fecha ele'/'abre de novo' (§18).

        kind distingue app/pasta/arquivo para que pronomes resolvam o alvo
        correto (fechar a janela do Explorer aberta por 'abre Downloads').
        """
        if not display_name:
            return
        context: dict[str, str] = {"display_name": display_name, "kind": kind}
        if process_names:
            context["process_names"] = "|".join(dict.fromkeys(process_names))
        if title_tokens:
            context["title_tokens"] = "|".join(dict.fromkeys(title_tokens))
        if path:
            context["path"] = path
        if hwnd:
            context["hwnd"] = str(hwnd)
        if canonical_id:
            context["canonical_id"] = canonical_id
        self.last_controlled = context

    def _context_hints(self) -> dict[str, list[str]]:
        """Dicas de resolução derivadas do último alvo controlado."""
        hints: dict[str, list[str]] = {}
        if not self.last_controlled:
            return hints
        process_names = self.last_controlled.get("process_names")
        if process_names:
            hints["process_names"] = [name for name in process_names.split("|") if name]
        title_tokens = self.last_controlled.get("title_tokens")
        if title_tokens:
            hints["title_tokens"] = [token for token in title_tokens.split("|") if token]
        return hints

    def _dedup_reply(self, turn_id: str | None, key: tuple[str, str]) -> tuple[bool, str] | None:
        if not turn_id:
            return None
        cached = self._universal_dedup.get((turn_id, *key))
        return (True, cached) if cached is not None else None

    def _remember_dedup(self, turn_id: str | None, key: tuple[str, str], reply: str) -> None:
        if not turn_id:
            return
        if len(self._universal_dedup) > 200:
            for stale in list(self._universal_dedup)[:100]:
                self._universal_dedup.pop(stale, None)
        self._universal_dedup[(turn_id, *key)] = reply

    async def initialize(self) -> None:
        self.registry = load_desktop_apps(self.apps_path)

    # ------------------------------------------------------------------ query

    def spec(self, app_id: str) -> DesktopAppSpec | None:
        return self.registry.get(app_id)

    def resolve_registered_app_id(self, value: str) -> str | None:
        """Resolve an id, alias or human display name to one trusted registry entry."""
        if self.spec(value) is not None:
            return value
        needle = normalize(value)
        matches = []
        for spec in self.registry.valid_specs():
            names = {normalize(spec.id), normalize(spec.display_name)}
            names.update(normalize(alias) for alias in spec.aliases)
            if needle and needle in names:
                matches.append(spec.id)
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

    async def launch_dynamic(self, query: str, *, origin: str = "operator",
                             force_new: bool = False) -> dict:
        """Universal launch: free-text app request resolved by discovery (#56..#67).

        force_new (§17): só True quando o operador pediu explicitamente outra
        instância ("abre outro", "nova janela").
        """
        started = time.perf_counter()

        def done(**kwargs) -> dict:
            payload = operation_result(app=query.strip(), action="launch_dynamic", duration_ms=(time.perf_counter() - started) * 1000, **kwargs)
            # Fonte da verdade da chamada corrente. Sem isso o pipeline podia
            # consumir o resultado da ação anterior ao abrir apps dinamicamente.
            self.last_operation_result = payload
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
        identity = (
            await asyncio.to_thread(self.universal.resolve_identity, clean)
            if self.universal is not None else {"status": "NOT_FOUND"}
        )
        if identity.get("status") == "AMBIGUOUS" and float(identity.get("confidence") or 0) >= 1.0:
            options = [
                {"id": entry.app_id, "display_name": entry.display_name}
                for entry in identity.get("entries", [])[:4]
            ]
            return done(
                success=False,
                error_code="AMBIGUOUS_APPLICATION",
                message="A consulta corresponde a aplicativos canonicamente diferentes.",
                execution_success=False,
                effect_verified=False,
                verification_status="NOT_EXECUTED",
                detail={"options": options},
            )
        # Fast path do Universal Registry (nyra-full §41): alias aprendido/exato
        # resolve sem busca fuzzy completa. Resolução roda FORA do event loop:
        # cache expirado dispara reindex completo (Start Menu/PowerShell) e
        # travaria o backend inteiro inline (nyra-full §2 divergência real).
        fast_candidates = (
            await asyncio.to_thread(self.universal.resolve_launch_candidates, clean)
            if self.universal is not None else []
        )
        fast_candidate = fast_candidates[0] if fast_candidates else None
        fast_processes_before = (
            _matching_process_pids(fast_candidate)
            if fast_candidate is not None else set()
        )
        if fast_candidate is not None and not force_new:
            existing = self._existing_instance_windows(fast_candidate)
            # Só focamos janelas VISÍVEIS. Instância oculta em tray segue o
            # caminho normal de launch: o próprio app traz a janela principal
            # à tona (focar superfícies ocultas trava em apps Electron).
            if existing and existing[0].visible:
                from app.desktop import window_manager as _wm

                focused = await asyncio.to_thread(_wm.focus_window, existing[0].hwnd)
                if focused:
                    await self._publish_verified(fast_candidate, existing[0].pid)
                    self.universal.record_success(fast_candidate.id, alias_query=clean)
                    self._note_controlled(
                        fast_candidate.display_name,
                        kind="app",
                        process_names=[existing[0].process_name or ""],
                        title_tokens=[fast_candidate.display_name],
                        hwnd=existing[0].hwnd,
                        canonical_id=fast_candidate.id,
                    )
                    return done(
                        success=True,
                        message=(f"'{fast_candidate.display_name}' já estava aberto; "
                                 f"janela existente em primeiro plano (pid {existing[0].pid})."),
                        execution_success=True, effect_verified=True,
                        verification_status="VERIFIED",
                        detail={"candidate": fast_candidate.public_dict(),
                                "pid": existing[0].pid, "already_open": True,
                                "windows": [w.model_dump(mode="json") for w in existing[:5]]},
                    )
        if fast_candidate is not None:
            raw_result, successful_candidate = await self._launch_candidates_with_fallback(
                fast_candidates,
                origin=origin,
            )
            if successful_candidate is not None:
                if fast_processes_before and raw_result.get("effect_verified") is True:
                    raw_result["already_open"] = True
                    raw_result["pre_existing_pids"] = sorted(fast_processes_before)
                self.universal.record_success(
                    successful_candidate.id,
                    alias_query=clean,
                    launch_candidate=successful_candidate,
                )
                result_windows = raw_result.get("windows") or []
                result_hwnd = int(result_windows[0].get("hwnd") or 0) if result_windows else 0
                self._note_controlled(
                    successful_candidate.display_name,
                    process_names=successful_candidate.process_names,
                    title_tokens=(successful_candidate.display_name,),
                    hwnd=result_hwnd or None,
                    canonical_id=successful_candidate.id,
                )
            else:
                self.universal.record_failure(fast_candidate.id)
            return self._finish_dynamic_attempt(done, raw_result)

        resolution = await asyncio.to_thread(self.discovery.resolve, clean)
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
                process_names=tuple(candidate_data.get("process_names") or ()),
                aliases=tuple(candidate_data.get("aliases") or ()),
            )
            if self.universal is not None:
                launch_candidates = await asyncio.to_thread(
                    self.universal.resolve_launch_candidates,
                    clean,
                    fallback=candidate,
                )
            else:
                from app.desktop.launch_policy import ordered_launch_candidates

                launch_candidates = ordered_launch_candidates([
                    *self.discovery.candidates_for(candidate.id),
                    candidate,
                ])
            if launch_candidates:
                candidate = launch_candidates[0]
            if not force_new:
                existing = self._existing_instance_windows(candidate)
                if existing and existing[0].visible:
                    from app.desktop import window_manager as _wm

                    focused = await asyncio.to_thread(_wm.focus_window, existing[0].hwnd)
                    if focused:
                        await self._publish_verified(candidate, existing[0].pid)
                        self.universal.record_success(candidate.id, alias_query=clean)
                        self._note_controlled(
                            candidate.display_name,
                            kind="app",
                            process_names=[existing[0].process_name or ""],
                            title_tokens=[candidate.display_name],
                            hwnd=existing[0].hwnd,
                            canonical_id=candidate.id,
                        )
                        return done(
                            success=True,
                            message=(f"'{candidate.display_name}' já estava aberto; "
                                     f"janela existente em primeiro plano (pid {existing[0].pid})."),
                            execution_success=True, effect_verified=True,
                            verification_status="VERIFIED",
                            detail={"candidate": candidate.public_dict(),
                                    "pid": existing[0].pid, "already_open": True,
                                    "windows": [w.model_dump(mode="json") for w in existing[:5]]},
                        )
            raw_result, successful_candidate = await self._launch_candidates_with_fallback(
                launch_candidates,
                origin=origin,
            )
            if successful_candidate is not None:
                if self.universal is not None:
                    self.universal.record_success(
                        successful_candidate.id,
                        alias_query=clean,
                        launch_candidate=successful_candidate,
                    )
                result_windows = raw_result.get("windows") or []
                result_hwnd = int(result_windows[0].get("hwnd") or 0) if result_windows else 0
                self._note_controlled(
                    successful_candidate.display_name,
                    process_names=successful_candidate.process_names,
                    title_tokens=(successful_candidate.display_name,),
                    hwnd=result_hwnd or None,
                    canonical_id=successful_candidate.id,
                )
            elif self.universal is not None:
                self.universal.record_failure(candidate.id)
            return self._finish_dynamic_attempt(done, raw_result)
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

    @staticmethod
    def _finish_dynamic_attempt(done, result: dict) -> dict:
        base_keys = {
            "success", "app", "action", "error_code", "message", "duration_ms",
            "execution_success", "effect_verified", "verification_status",
        }
        detail = {key: value for key, value in result.items() if key not in base_keys}
        return done(
            success=bool(result.get("success")),
            error_code=result.get("error_code"),
            message=str(result.get("message") or ""),
            execution_success=result.get("execution_success"),
            effect_verified=result.get("effect_verified"),
            verification_status=str(result.get("verification_status") or "NOT_REQUIRED"),
            detail=detail,
        )

    async def _launch_candidates_with_fallback(
        self,
        candidates: list[ApplicationCandidate],
        *,
        origin: str,
    ) -> tuple[dict, ApplicationCandidate | None]:
        """Try every discovered route until PID/HWND verification succeeds."""
        attempts: list[dict] = []
        last_result: dict | None = None
        for candidate in candidates:
            def attempt_done(**kwargs) -> dict:
                return operation_result(
                    app=candidate.display_name,
                    action="launch_attempt",
                    **kwargs,
                )

            result = await self._launch_candidate(
                candidate,
                attempt_done,
                origin=origin,
                expected_window=True,
            )
            attempt = {
                "launch_method": str(candidate.launch_method),
                "source": candidate.source,
                "target": candidate.public_dict()["target"],
                "execution_success": result.get("execution_success"),
                "effect_verified": result.get("effect_verified"),
                "verification_status": result.get("verification_status"),
                "error_code": result.get("error_code"),
            }
            attempts.append(attempt)
            result["attempts"] = list(attempts)
            result["launch_method"] = str(candidate.launch_method)
            result["launch_source"] = candidate.source
            last_result = result
            if result.get("success") and result.get("effect_verified") is True:
                return result, candidate

        if last_result is None:
            last_result = operation_result(
                success=False,
                app="",
                action="launch_attempt",
                error_code=LaunchErrorCode.EXECUTABLE_NOT_FOUND.value,
                message="No valid local launch method was discovered.",
                execution_success=False,
                effect_verified=False,
                verification_status="EXECUTION_FAILED",
                detail={"attempts": []},
            )
        else:
            last_message = str(last_result.get("message") or "")
            last_result["success"] = False
            last_result["effect_verified"] = False
            last_result["verification_status"] = "VERIFICATION_FAILED"
            last_result["message"] = (
                f"Todos os {len(attempts)} metodos locais foram tentados sem "
                f"confirmacao por PID/HWND. Ultima falha: {last_message}"
            )
        return last_result, None

    def _visible_windows_for_candidate(self, candidate: ApplicationCandidate) -> list[WindowInfo]:
        """Janelas visíveis já existentes do candidato (§17 already-open)."""
        return [
            window for window in annotate_process_names(list_visible_windows())
            if _window_relevant(window, candidate) and not _is_protected_window(window)
        ]

    def _existing_instance_windows(self, candidate: ApplicationCandidate) -> list[WindowInfo]:
        """Instância existente, VISÍVEL ou oculta em tray (§17/§26).

        Janela oculta só casa por NOME DE PROCESSO (sinal forte); título de
        janela invisível não é evidência confiável. Stems vêm do alvo do
        candidato E do registro universal (exe/atalho), cobrindo apps que
        resolvem via lnk/AUMID (Discord, Steam).
        """
        visible = self._visible_windows_for_candidate(candidate)
        if visible:
            return visible
        stems: set[str] = set()
        try:
            stem = Path(expand_launch_target(candidate.target)).stem.casefold()
            if stem and stem not in {"application", "app"}:
                stems.add(stem)
        except OSError:
            pass
        entry = self.universal.entries.get(candidate.id) if self.universal else None
        if entry is not None:
            if entry.executable:
                stems.add(Path(entry.executable).stem.casefold())
            if entry.target:
                stems.add(Path(entry.target).stem.casefold())
        stems.discard("")
        if not stems:
            return []
        hidden: list[WindowInfo] = []
        for window in annotate_process_names(list_application_windows(include_invisible=True)):
            if _is_protected_window(window):
                continue
            process_name = (window.process_name or "").casefold().removesuffix(".exe")
            if process_name in stems:
                hidden.append(window)
        # Janelas COM título primeiro (superfícies de rendering não têm título).
        hidden.sort(key=lambda item: not bool((item.title or "").strip()))
        return hidden[:1]

    async def _launch_candidate(self, candidate: ApplicationCandidate, done, *, origin: str, expected_window: bool = True) -> dict:
        method = candidate.launch_method
        pre_existing = {
            window.hwnd for window in annotate_process_names(list_visible_windows())
            if _window_relevant(window, candidate)
        }
        pre_existing_pids = _matching_process_pids(candidate)
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
                if _is_console_exe(executable):
                    return done(
                        success=False,
                        error_code=LaunchErrorCode.SPAWN_FAILED.value,
                        message=(
                            f"'{candidate.display_name}' requer ShellExecute para "
                            "preservar a janela de console."
                        ),
                        execution_success=False,
                        effect_verified=False,
                        verification_status="EXECUTION_FAILED",
                        detail={"candidate": candidate.public_dict()},
                    )
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
                            and window.hwnd not in pre_existing
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
                    # App de fundo (tray): sem janela OBRIGATÓRIA, mas se uma
                    # janela relevante surgir, confirma efeito real (§14).
                    background_deadline = time.monotonic() + 8.0
                    while time.monotonic() < background_deadline:
                        await asyncio.sleep(0.5)
                        found_background = [
                            window for window in annotate_process_names(list_visible_windows())
                            if _window_relevant(window, candidate)
                            and window.hwnd not in pre_existing
                            and not _is_protected_window(window)
                        ]
                        if found_background:
                            await self._publish_verified(candidate, found_background[0].pid)
                            return done(success=True,
                                        message=f"'{candidate.display_name}' aberto; janela visível confirmada (pid {found_background[0].pid}).",
                                        execution_success=True, effect_verified=True,
                                        verification_status="VERIFIED",
                                        detail={"candidate": candidate.public_dict(), "pid": process.pid,
                                                "windows": [w.model_dump(mode="json") for w in found_background[:5]]})
                    alive = poll_process() is None
                    return done(success=alive, message="Processo em background iniciado; verificação por janela não aplicável.",
                                execution_success=True, effect_verified=alive, verification_status="VERIFIED" if alive else "VERIFICATION_FAILED",
                                detail={"candidate": candidate.public_dict(), "pid": process.pid})
                if not confirmed:
                    confirmed_pids = _matching_process_pids(candidate) - pre_existing_pids
                    if confirmed_pids:
                        verified_pid = min(confirmed_pids)
                        await self._publish_verified(candidate, verified_pid)
                        return done(
                            success=True,
                            message=(
                                f"'{candidate.display_name}' aberto via {method}; "
                                f"processo confirmado (pid {verified_pid})."
                            ),
                            execution_success=True,
                            effect_verified=True,
                            verification_status="VERIFIED",
                            detail={
                                "candidate": candidate.public_dict(),
                                "pid": verified_pid,
                                "verification": "PID",
                            },
                        )
                    # nyra-full §11: app single-instance já aberto não é falha —
                    # se as janelas pré-existentes seguem vivas, foca e reporta.
                    still_open = [
                        window for window in annotate_process_names(list_visible_windows())
                        if _window_relevant(window, candidate)
                        and window.hwnd in pre_existing
                        and not _is_protected_window(window)
                    ]
                    if still_open:
                        from app.desktop import window_manager as _wm

                        _wm.focus_window(still_open[0].hwnd)
                        await self._publish_verified(candidate, still_open[0].pid)
                        return done(success=True,
                                    message=f"'{candidate.display_name}' já estava aberto; janela existente trazida para frente (pid {still_open[0].pid}).",
                                    execution_success=True, effect_verified=True, verification_status="VERIFIED",
                                    detail={"candidate": candidate.public_dict(), "pid": still_open[0].pid,
                                            "already_open": True,
                                            "windows": [w.model_dump(mode="json") for w in still_open[:5]]})
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
            confirmed_pids: set[int] = set()
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                confirmed = [
                    window for window in annotate_process_names(list_visible_windows())
                    if _window_relevant(window, candidate) and window.hwnd not in pre_existing
                ]
                if confirmed:
                    break
                confirmed_pids = _matching_process_pids(candidate) - pre_existing_pids
                if confirmed_pids:
                    break
            if not expected_window:
                # Background/tray: confirmação oportunista de janela nova.
                background_deadline = time.monotonic() + 8.0
                while time.monotonic() < background_deadline:
                    await asyncio.sleep(0.5)
                    found_background = [
                        window for window in annotate_process_names(list_visible_windows())
                        if _window_relevant(window, candidate) and window.hwnd not in pre_existing
                        and not _is_protected_window(window)
                    ]
                    if found_background:
                        await self._publish_verified(candidate, found_background[0].pid)
                        return done(success=True,
                                    message=f"'{candidate.display_name}' aberto; janela visível confirmada (pid {found_background[0].pid}).",
                                    execution_success=True, effect_verified=True,
                                    verification_status="VERIFIED",
                                    detail={"candidate": candidate.public_dict(),
                                            "windows": [w.model_dump(mode="json") for w in found_background[:5]]})
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
            if confirmed_pids:
                verified_pid = min(confirmed_pids)
                await self._publish_verified(candidate, verified_pid)
                return done(
                    success=True,
                    message=(
                        f"'{candidate.display_name}' aberto via {method}; "
                        f"processo confirmado (pid {verified_pid})."
                    ),
                    execution_success=True,
                    effect_verified=True,
                    verification_status="VERIFIED",
                    detail={
                        "candidate": candidate.public_dict(),
                        "pid": verified_pid,
                        "verification": "PID",
                    },
                )
            still_open = [
                window for window in annotate_process_names(list_visible_windows())
                if _window_relevant(window, candidate)
                and window.hwnd in pre_existing
                and not _is_protected_window(window)
            ]
            if still_open:
                from app.desktop import window_manager as _wm

                _wm.focus_window(still_open[0].hwnd)
                await self._publish_verified(candidate, still_open[0].pid)
                return done(success=True,
                            message=f"'{candidate.display_name}' já estava aberto; janela existente trazida para frente.",
                            execution_success=True, effect_verified=True, verification_status="VERIFIED",
                            detail={"candidate": candidate.public_dict(), "pid": still_open[0].pid,
                                    "already_open": True,
                                    "windows": [w.model_dump(mode="json") for w in still_open[:5]]})
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
        if not resolved.is_file():
            return done(success=False, error_code="FILE_NOT_FOUND",
                        message=f"Arquivo inexistente: {resolved}", execution_success=False, effect_verified=False)
        if app and self.spec(app) is None:
            return done(success=False, error_code=LaunchErrorCode.UNKNOWN_APP.value,
                        message=f"Aplicativo '{app}' não registrado.", execution_success=False)
        if not app and resolved.suffix.casefold() not in _SAFE_ASSOCIATION_SUFFIXES:
            return done(
                success=False,
                error_code="UNSAFE_FILE_TYPE",
                message=(
                    "Tipo de arquivo não autorizado para associação automática. "
                    "Use um aplicativo do Desktop Apps Registry; executáveis, scripts "
                    "e atalhos passam somente por system_shell com approval."
                ),
                execution_success=False,
                effect_verified=False,
            )
        try:
            if app:
                executable = shutil.which(self.spec(app).executable) or expand_launch_target(self.spec(app).executable)
                creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                gui_env = os.environ.copy()
                gui_env.pop('ELECTRON_RUN_AS_NODE', None)
                subprocess.Popen(  # noqa: S603 - executável vem do registry confiável; caminho validado acima
                    [executable, str(resolved)], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=True, creationflags=creationflags, env=gui_env,
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
                    execution_success=True, effect_verified=None, verification_status="EXECUTED",
                    detail={"path": str(resolved), "with_app": app or None})

    async def open_url(self, url: str) -> dict:
        started = time.perf_counter()

        def done(**kwargs) -> dict:
            return operation_result(app="browser", action="open_url", duration_ms=(time.perf_counter() - started) * 1000, **kwargs)

        clean = url.strip()
        try:
            parsed = urlsplit(clean)
            _ = parsed.port
        except ValueError:
            parsed = None
        if (
            parsed is None
            or parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(ord(char) < 0x20 for char in clean)
        ):
            return done(success=False, error_code="INVALID_URL",
                        message="Somente URLs HTTP/HTTPS absolutas, sem credenciais embutidas, são aceitas.",
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
                    execution_success=True, effect_verified=None, verification_status="EXECUTED",
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

    def _require_uia_approval(self, action: str, parameters: dict,
                              approval_id: str | None) -> dict | None:
        """One-use approval bound to the exact UI actuator and window."""
        from app.tools.shell_models import ShellRiskLevel

        canonical = json.dumps(
            parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        binding_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        description = f"desktop_ui_{action} params_sha256={binding_digest}"
        target = f"desktop:ui:{action}:{parameters.get('hwnd', '')}"
        if self.approvals is None:
            return {"success": False, "error_code": "APPROVAL_REQUIRED",
                    "approval_required": True}
        fingerprint = self.approvals.fingerprint(
            description, "desktop_ui", "", 30, target=target,
        )
        if not approval_id:
            record = self.approvals.request(
                command=description, shell="desktop_ui", working_directory="",
                timeout_seconds=30, risk_level=ShellRiskLevel.ELEVATED,
                target=target, fingerprint=fingerprint,
            )
            return {"success": False, "error_code": "APPROVAL_REQUIRED",
                    "approval_required": True, "approval_id": record.approval_id}
        granted, reason = self.approvals.consume(approval_id, fingerprint)
        if not granted:
            return {"success": False, "error_code": "APPROVAL_INVALID", "message": reason}
        return None

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
                       app: str = "", query: str = "", hwnd: int | None = None,
                       approval_id: str | None = None) -> dict:
        from app.desktop import uia

        try:
            handle = self._window_hwnd_for_uia(app=app, query=query, hwnd=hwnd)
        except ValueError as exc:
            code, message = str(exc).split(":", 1)
            return operation_result(app="ui", action="click", success=False, error_code=code, message=message,
                                    execution_success=False, effect_verified=False)
        decision = self._require_uia_approval(
            "click",
            {"hwnd": handle, "name": name, "automation_id": automation_id,
             "control_type": control_type},
            approval_id,
        )
        if decision is not None:
            return decision
        return await self._uia_call(
            uia.click_element, handle, name=name, automation_id=automation_id, control_type=control_type,
        )

    async def ui_set_text(self, value: str, *, name: str = "", automation_id: str = "", control_type: str = "",
                          app: str = "", query: str = "", hwnd: int | None = None,
                          approval_id: str | None = None) -> dict:
        from app.desktop import uia

        try:
            handle = self._window_hwnd_for_uia(app=app, query=query, hwnd=hwnd)
        except ValueError as exc:
            code, message = str(exc).split(":", 1)
            return operation_result(app="ui", action="set_text", success=False, error_code=code, message=message,
                                    execution_success=False, effect_verified=False)
        decision = self._require_uia_approval(
            "set_text",
            {"hwnd": handle, "name": name, "automation_id": automation_id,
             "control_type": control_type,
             "value_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()},
            approval_id,
        )
        if decision is not None:
            return decision
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

    async def ui_send_keys(self, text: str, *, app: str = "", query: str = "", hwnd: int | None = None,
                           approval_id: str | None = None) -> dict:
        from app.desktop import uia
        from app.desktop.window_manager import focus_window

        try:
            handle = self._window_hwnd_for_uia(app=app, query=query, hwnd=hwnd)
        except ValueError as exc:
            code, message = str(exc).split(":", 1)
            return operation_result(app="ui", action="send_keys", success=False, error_code=code, message=message,
                                    execution_success=False, effect_verified=False)
        decision = self._require_uia_approval(
            "send_keys",
            {"hwnd": handle,
             "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()},
            approval_id,
        )
        if decision is not None:
            return decision
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

    # ==================================================== Universal Operator

    def universal_status(self) -> dict:
        status = self.universal.status()
        status["last_controlled"] = dict(self.last_controlled) if self.last_controlled else None
        return status

    def refresh_universal(self, force: bool = True) -> dict:
        sources = self.universal.refresh(force=force)
        return {"refreshed": True, "sources": sources, **self.universal_status()}

    async def handle_universal(self, intent, *, turn_id: str | None = None) -> tuple[bool, str]:
        """Executa uma UniversalIntent SEM LLM (nyra-full §25/§41).

        Retorna (handled, reply). Dedup por turno garante 1 pedido →
        1 execução física mesmo se chamado duas vezes.
        """
        cached = self._dedup_reply(turn_id, intent.dedup_key)
        if cached is not None:
            return cached

        # Nova ação nunca herda evidência de uma ação física anterior.
        self.last_operation_result = None

        target_query = intent.target
        hints: dict[str, list[str]] = {}
        if intent.contextual:
            context = self.last_controlled
            if not context:
                reply = "Não sei a qual item você se refere — mencione o nome dele."
                self._remember_dedup(turn_id, intent.dedup_key, reply)
                return True, reply
            target_query = context.get("display_name") or target_query
            hints = self._context_hints()

        action_value = intent.action.value
        if action_value == "OPEN_APP":
            force_new = bool(getattr(intent, "explicit_new", False))
            if intent.contextual and (self.last_controlled or {}).get("kind") == "folder":
                handled, reply = await self._universal_open_folder(target_query)
            elif intent.contextual and (self.last_controlled or {}).get("kind") == "file":
                handled, reply = await self._universal_open_file(
                    (self.last_controlled or {}).get("path") or target_query, contextual=True
                )
            else:
                handled, reply = await self._universal_open(target_query, force_new=force_new)
        elif action_value == "OPEN_FOLDER":
            handled, reply = await self._universal_open_folder(target_query)
        elif action_value == "OPEN_FILE":
            handled, reply = await self._universal_open_file(target_query, contextual=intent.contextual)
        else:
            handled, reply = await self._universal_window_op(action_value, target_query, hints=hints)

        if self.last_operation_result is not None:
            from app.desktop.presenter import ActionResultPresenter

            user_facing = ActionResultPresenter.present(
                self.last_operation_result,
                requested_action=action_value,
                requested_app=target_query,
            )
            if user_facing:
                self.last_operation_result["user_facing_response"] = user_facing
                reply = user_facing

        self._remember_dedup(turn_id, intent.dedup_key, reply)
        return handled, reply

    async def _universal_open_folder(self, name_query: str) -> tuple[bool, str]:
        """OPEN_FOLDER determinístico (nyra-full §6): resolve pasta conhecida,
        abre via Explorer/ShellExecute e verifica a janela. NUNCA usa
        filesystem_list_files nem Agent Loop."""
        from app.desktop.intents import FOLDER_SHELL_URIS

        explicit = Path(name_query.strip().strip('"')).expanduser()
        explicit_folder = (
            explicit.resolve()
            if explicit.is_absolute() and explicit.is_dir()
            else None
        )
        key = normalize(name_query)
        resolved: tuple[str, str] | None = None  # (display_name, uri_or_path)
        entry = FOLDER_SHELL_URIS.get(key)
        if explicit_folder is not None:
            resolved = (
                explicit_folder.name or str(explicit_folder),
                str(explicit_folder),
            )
        elif entry is not None:
            resolved = entry
        elif key == "home":
            resolved = ("Home", str(Path.home()))
        elif key == "appdata":
            roaming = os.environ.get("APPDATA")
            if roaming:
                resolved = ("AppData", roaming)
        elif key == "appdatelocal":
            local = os.environ.get("LOCALAPPDATA")
            if local:
                resolved = ("AppData Local", local)
        elif key in {"temp", "temporarios"}:
            temp_dir = os.environ.get("TEMP") or os.environ.get("TMP")
            if temp_dir:
                resolved = ("Temp", temp_dir)
        elif key == "onedrive":
            onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
            if onedrive:
                resolved = ("OneDrive", onedrive)
        else:
            # Pasta do usuário: procura por nome sob raízes conhecidas.
            resolved = self._find_user_folder(name_query)

        if resolved is None:
            return True, (
                f"Não encontrei a pasta \"{name_query}\" nos locais conhecidos. "
                "Nada foi aberto."
            )

        display_name, target_uri = resolved
        candidate = ApplicationCandidate(
            id=f"folder_{normalize(display_name)}",
            display_name=display_name,
            source="folder_alias",
            launch_method=LaunchMethod.URI,
            target=target_uri,
            confidence=1.0,
        )
        started = time.perf_counter()

        def done(**kwargs) -> dict:
            return operation_result(app=display_name, action="universal_open_folder",
                                    duration_ms=(time.perf_counter() - started) * 1000, **kwargs)

        result = await self._launch_candidate(candidate, done, origin="fastpath")
        result["subject_kind"] = "folder"
        result.pop("user_facing_response", None)
        self.last_operation_result = result
        if result.get("already_open"):
            self._note_controlled(display_name, kind="folder",
                                  process_names=("explorer",), title_tokens=(display_name,),
                                  path=None if target_uri.startswith("shell:") else target_uri)
            return True, f"Pasta {display_name} já estava aberta; janela existente em primeiro plano."
        if result.get("success") and result.get("effect_verified"):
            self._note_controlled(display_name, kind="folder",
                                  process_names=("explorer",), title_tokens=(display_name,),
                                  path=None if target_uri.startswith("shell:") else target_uri)
            return True, f"Pasta {display_name} aberta no Explorador."
        if result.get("success"):
            self._note_controlled(display_name, kind="folder",
                                  process_names=("explorer",), title_tokens=(display_name,),
                                  path=None if target_uri.startswith("shell:") else target_uri)
            return True, f"Comando da pasta {display_name} enviado; janela ainda não confirmada."
        return True, f"Não consegui abrir a pasta {display_name}: {result.get('message', '')}"

    def _find_user_folder(self, name_query: str) -> tuple[str, str] | None:
        """Localiza pasta do usuário pelo nome sob raízes comuns (sem hardcode)."""
        wanted = normalize(name_query)
        if not wanted:
            return None
        home = Path.home()
        roots = [home / child for child in
                 ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")]
        roots.extend([home, DATA_ROOT, PROJECT_ROOT])
        for root in roots:
            try:
                if not root.is_dir():
                    continue
                for child in sorted(root.iterdir()):
                    if child.is_dir() and normalize(child.name) == wanted:
                        return child.name, str(child)
            except OSError:
                continue
        return None

    def _find_file_by_name(self, raw_name: str) -> Path | None:
        """Resolve arquivo por nome em locais padrão (nyra-full §7/§29)."""
        clean = raw_name.strip().strip('"')
        if not clean:
            return None
        candidate = Path(clean).expanduser()
        if candidate.is_absolute():
            return candidate if candidate.is_file() else None
        for base in (PROJECT_ROOT, DATA_ROOT):
            direct = base / candidate
            if direct.is_file():
                return direct
        filename = candidate.name.casefold()
        home = Path.home()
        roots = [home / child for child in
                 ("Desktop", "Downloads", "Documents", "Pictures", "Music", "Videos")]
        roots.extend([DATA_ROOT, PROJECT_ROOT])
        for root in roots:
            try:
                if not root.is_dir():
                    continue
                for child in sorted(root.iterdir()):
                    if child.is_file() and child.name.casefold() == filename:
                        return child
            except OSError:
                continue
        return None

    async def _universal_open_file(self, raw_name: str, *, contextual: bool = False) -> tuple[bool, str]:
        """OPEN_FILE determinístico (nyra-full §7): resolve → app associado →
        abrir → verificar janela/arquivo quando possível."""
        from app.desktop import window_manager as wm

        resolved_path: Path | None
        if contextual:
            context = self.last_controlled or {}
            raw = context.get("path") if context.get("kind") == "file" else None
            if not raw:
                return True, "Não sei a qual arquivo você se refere — cite o nome dele."
            resolved_path = Path(raw)
            if not resolved_path.is_file():
                resolved_path = self._find_file_by_name(resolved_path.name)
        else:
            resolved_path = self._find_file_by_name(raw_name)
        if resolved_path is None or not resolved_path.is_file():
            return True, (
                f"Não encontrei o arquivo \"{raw_name}\" nos locais padrão. "
                "Nada foi aberto."
            )

        pre_existing = {
            window.hwnd for window in annotate_process_names(list_visible_windows())
        }
        result = await self.open_file(str(resolved_path))
        self.last_operation_result = result
        if not result.get("success"):
            return True, (
                f"Não consegui abrir o arquivo {resolved_path.name}: {result.get('message', '')}"
            )

        stem = resolved_path.stem.casefold()
        deadline = time.monotonic() + 10.0
        confirmed = None
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            for window in annotate_process_names(list_visible_windows()):
                if window.hwnd in pre_existing or _is_protected_window(window):
                    continue
                if stem and stem in (window.title or "").casefold():
                    confirmed = window
                    break
            if confirmed is not None:
                break
        if confirmed is not None:
            result = {**result, "effect_verified": True, "already_open": False,
                      "windows": [confirmed.model_dump(mode="json")]}
            self.last_operation_result = result
            self._note_controlled(resolved_path.stem, kind="file",
                                  title_tokens=(resolved_path.stem,), path=str(resolved_path))
            process_label = (confirmed.process_name or "aplicativo associado").removesuffix(".exe")
            return True, (
                f"Arquivo {resolved_path.name} aberto no {process_label} "
                f"(janela hwnd {confirmed.hwnd})."
            )
        # Arquivo pode já estar aberto: foca a janela existente correspondente.
        for window in annotate_process_names(list_visible_windows()):
            if _is_protected_window(window):
                continue
            if stem and stem in (window.title or "").casefold():
                focused = await asyncio.to_thread(wm.focus_window, window.hwnd)
                if focused:
                    self._note_controlled(resolved_path.stem, kind="file",
                                          title_tokens=(resolved_path.stem,), path=str(resolved_path))
                    return True, (
                        f"Arquivo {resolved_path.name} já estava aberto; "
                        "janela existente trazida para frente."
                    )
        return True, (
            f"Comando de abertura de {resolved_path.name} enviado; "
            "não consegui confirmar a janela do aplicativo."
        )

    async def _universal_open(self, target_query: str, *, force_new: bool = False) -> tuple[bool, str]:
        cleaned = target_query
        if cleaned.casefold().startswith("pasta "):
            # Rede de segurança: frase de pasta que escapou do parser (§6).
            return await self._universal_open_folder(cleaned[6:].strip())

        result = await self.launch_dynamic(cleaned, origin="fastpath", force_new=force_new)
        error_code = str(result.get("error_code") or "")
        if result.get("success"):
            detail_pid = result.get("pid")
            windows = result.get("windows") or []
            hwnd_text = ""
            if windows and isinstance(windows, list):
                hwnd_text = f", janela {windows[0].get('hwnd')}" if windows[0].get("hwnd") else ""
            verified = bool(result.get("effect_verified"))
            name = result.get("app") or cleaned
            # Contexto (§18) vale para TODO sucesso — inclusive abertura em
            # background/tray sem confirmação de janela.
            result_windows = result.get("windows") or []
            result_candidate = result.get("candidate") or {}
            self._note_controlled(
                name,
                kind="app",
                process_names=tuple(result_candidate.get("process_names") or ()),
                title_tokens=(name,),
                hwnd=(int(result_windows[0].get("hwnd") or 0) or None)
                if result_windows else None,
                canonical_id=str(result_candidate.get("id") or ""),
            )
            if result.get("already_open"):
                return True, f"{name} já estava aberto; janela existente em primeiro plano."
            if verified:
                pid_text = f" (PID {detail_pid})" if detail_pid else ""
                return True, f"Aberto: {name}{pid_text}{hwnd_text}."
            return True, f"Comando de abertura de {name} enviado, mas ainda não consegui confirmar a janela."
        if error_code == "AMBIGUOUS_APPLICATION":
            options = result.get("options") or []
            names = [str(item.get("display_name")) for item in options[:4] if item.get("display_name")]
            question = " ou ".join(names) if names else "as opções disponíveis"
            return True, f"Há mais de um aplicativo possível: {question}? Diga qual prefere."
        if error_code in {LaunchErrorCode.EXECUTABLE_NOT_FOUND.value, LaunchErrorCode.UNKNOWN_APP.value}:
            return True, (
                f"Não encontrei nenhum aplicativo instalado correspondente a \"{target_query}\". "
                "Nada foi executado."
            )
        return True, f"Falha ao abrir {target_query}: {result.get('message', 'erro desconhecido')}"

    def _resolve_window_targets(self, query: str, hints: dict[str, list[str]] | None = None) -> list[WindowInfo]:
        """Janelas visíveis do alvo consultado (registry + universal + título +
        dicas de contexto para pronomes como 'ele'/'ela' — nyra-full §8/§18)."""
        candidates: list[str] = []          # nomes de processo sem .exe
        title_tokens: list[str] = []

        if hints:
            candidates.extend(name.casefold().removesuffix(".exe") for name in hints.get("process_names", []))
            title_tokens.extend(token.casefold() for token in hints.get("title_tokens", []))

        spec_id = self.resolve_registered_app_id(query)
        if spec_id:
            spec = self.spec(spec_id)
            if spec:
                candidates.extend(name.removesuffix(".exe") for name in spec.normalized_process_names())
                title_tokens.extend(token.casefold() for token in spec.window_title_contains)

        query_norm = normalize(query)
        entry = self.universal.entries.get(query_norm)
        if entry is None:
            for candidate_entry in self.universal.entries.values():
                alias_set = {normalize(alias.removeprefix("learned:")) for alias in candidate_entry.aliases}
                if query_norm and query_norm in alias_set:
                    entry = candidate_entry
                    break
        if entry is not None:
            candidates.extend(self.universal.process_names_for(entry.app_id))
            exe_stem = Path(entry.executable).stem.casefold() if entry.executable else ""
            if exe_stem:
                candidates.append(exe_stem)
            title_tokens.append(entry.display_name.casefold())

        plain_stem = normalize(Path(query.strip()).stem)
        if plain_stem:
            candidates.append(plain_stem)

        candidates = [name for name in {name.casefold() for name in candidates if name}]
        matches: list[WindowInfo] = []
        seen_hwnd: set[int] = set()
        for window in annotate_process_names(list_visible_windows()):
            if window.hwnd in seen_hwnd or _is_protected_window(window):
                continue
            process_name = (window.process_name or "").casefold().removesuffix(".exe")
            title = (window.title or "").casefold()
            if process_name in candidates or any(token and token in title for token in title_tokens):
                matches.append(window)
                seen_hwnd.add(window.hwnd)
        return matches

    async def _universal_window_op(self, action_value: str, target_query: str,
                                   *, hints: dict[str, list[str]] | None = None) -> tuple[bool, str]:
        from app.desktop import window_manager as wm

        windows = self._resolve_window_targets(target_query, hints=hints)
        if not windows:
            result_payload = operation_result(
                app=target_query,
                action=f"universal_{action_value.casefold()}",
                success=False,
                error_code="WINDOW_NOT_FOUND",
                message=(
                    f"Nenhuma janela visível de \"{target_query}\" neste momento — nada foi alterado."
                ),
                execution_success=False,
                effect_verified=False,
                verification_status="NOT_EXECUTED",
            )
            self.last_operation_result = result_payload
            return True, str(result_payload["message"])

        affected = 0
        details: list[str] = []
        for window in windows[:5]:
            ok = False
            if action_value == "CLOSE_APP":
                ok = wm.graceful_close(window.hwnd)
            elif action_value == "MINIMIZE_APP":
                ok = wm.minimize_window(window.hwnd)
            elif action_value == "MAXIMIZE_APP":
                ok = wm.maximize_window(window.hwnd)
            elif action_value == "RESTORE_APP":
                ok = wm.restore_window(window.hwnd)
            elif action_value == "FOCUS_APP":
                ok = wm.focus_window(window.hwnd)
            elif action_value == "SWITCH_APP":
                # Alternar: se estiver minimizada restaura; senão traz à frente.
                if wm.window_state(window.hwnd)["iconic"]:
                    ok = wm.restore_window(window.hwnd)
                    ok = bool(wm.focus_window(window.hwnd)) or ok
                else:
                    ok = wm.focus_window(window.hwnd)
            if ok:
                affected += 1
                details.append(f"hwnd={window.hwnd}")
        verb = {
            "CLOSE_APP": "fechada(s)",
            "MINIMIZE_APP": "minimizada(s)",
            "MAXIMIZE_APP": "maximizada(s)",
            "RESTORE_APP": "restaurada(s)",
            "FOCUS_APP": "trazida(s) para frente",
            "SWITCH_APP": "colocada(s) em primeiro plano",
        }.get(action_value, "alterada(s)")
        # Um alvo explícito nunca herda o rótulo/path do último contexto.
        # Isso evitava fechar corretamente o Notepad e responder "Downloads".
        context = (self.last_controlled or {}) if hints else {}
        label = context.get("display_name") or target_query
        kind = context.get("kind", "app")
        result_payload = operation_result(
            app=label, action=f"universal_{action_value.casefold()}",
            success=affected > 0, message="; ".join(details),
            execution_success=affected > 0,
            effect_verified=bool(affected),
            verification_status="VERIFIED" if affected else "VERIFICATION_FAILED",
            subject_kind=kind,
            windows=[w.model_dump(mode="json") for w in windows[:5]],
        )
        self.last_operation_result = result_payload
        if affected:
            self._note_controlled(
                label, kind=kind,
                process_names=[(w.process_name or "").casefold().removesuffix(".exe") for w in windows if w.process_name],
                title_tokens=[label.casefold()],
                path=context.get("path"),
            )
            return True, (
                f"{affected} janela(s) de {label} {verb} com verificação ({'; '.join(details)})."
            )
        return True, (
            f"Encontrei {len(windows)} janela(s) de {label}, mas nenhuma ação foi confirmada pelo sistema."
        )
