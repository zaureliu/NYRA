"""Camada 5 — EffectVerificationService (kazumi-7c §40-§49).

A KAZUMI só considera operação concluída quando o efeito esperado é COMPROVADO
por fonte determinística (Win32/UIA/filesystem/processo/browser/homelab).
`verified=None` significa "não consegui provar" → resposta honesta (§49).

Este serviço NÃO duplica as verificações do DesktopController: ele as
CONSOME (`from_operation_result`) e adiciona probes independentes para
janela/arquivo/processo quando o chamador precisa revalidar.
"""

from __future__ import annotations

import ctypes
import time
from typing import Any, Callable
from uuid import uuid4

from pydantic import BaseModel, Field


class VerifiedEffect(BaseModel):
    action_id: str = Field(default_factory=lambda: f"act_{uuid4().hex[:12]}")
    expected: str = ""
    observed: str = ""
    source: str = "win32"
    verified: bool | None = None       # None = UNKNOWN (§49)
    confidence: float = 0.0
    verified_at: float = Field(default_factory=time.time)
    details: dict[str, Any] = Field(default_factory=dict)


def _foreground_hwnd() -> int:
    try:
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001
        return 0


class EffectVerificationService:
    """Probes de efeito real; todos com timeout implícito de uma leitura."""

    def verify_window(self, hwnd: int, *, expect_minimized: bool | None = None,
                      expect_maximized: bool | None = None,
                      expect_foreground: bool | None = None,
                      expect_gone: bool | None = None,
                      timeout_seconds: float = 3.0) -> VerifiedEffect:
        from app.desktop.window_manager import wait_for, window_state

        expectations: list[str] = []
        if expect_minimized is not None:
            expectations.append(f"minimized={expect_minimized}")
        if expect_maximized is not None:
            expectations.append(f"maximized={expect_maximized}")
        if expect_foreground is not None:
            expectations.append(f"foreground={expect_foreground}")
        if expect_gone:
            expectations.append("window gone/hidden")

        def probe() -> tuple[bool | None, str]:
            try:
                state = window_state(hwnd)
            except Exception:  # noqa: BLE001
                return None, "state read failed"
            if not state.get("alive") or not state.get("visible"):
                ok = bool(expect_gone)
                return ok, f"alive={bool(state.get('alive'))} visible={bool(state.get('visible'))}"
            if expect_gone:
                return False, "janela ainda visível"
            checks: list[bool] = []
            observed: list[str] = []
            if expect_minimized is not None:
                checks.append(bool(state["iconic"]) == expect_minimized)
                observed.append(f"iconic={state['iconic']}")
            if expect_maximized is not None:
                checks.append(bool(state["zoomed"]) == expect_maximized)
                observed.append(f"zoomed={state['zoomed']}")
            if expect_foreground is not None:
                checks.append(bool(state["foreground"]) == expect_foreground)
                observed.append(f"foreground={state['foreground']}")
            return all(checks), "; ".join(observed)

        deadline = time.monotonic() + timeout_seconds
        verified: bool | None = False
        observed = ""
        while True:
            verified, observed = probe()
            if verified or time.monotonic() >= deadline:
                break
            time.sleep(0.15)
        return VerifiedEffect(
            expected=", ".join(expectations) or "estado da janela",
            observed=observed, source="win32", verified=verified,
            confidence=1.0 if verified else 0.4,
            details={"hwnd": hwnd},
        )

    def verify_app_visible(self, process_stem: str, *,
                           title_token: str | None = None) -> VerifiedEffect:
        from app.desktop.windows import annotate_process_names, list_visible_windows

        stem = process_stem.casefold().removesuffix(".exe")
        token = (title_token or "").casefold()
        matches = []
        for window in annotate_process_names(list_visible_windows()):
            proc = (window.process_name or "").casefold().removesuffix(".exe")
            title = (window.title or "").casefold()
            if proc == stem and (not token or token in title):
                matches.append({"hwnd": window.hwnd, "pid": window.pid,
                                "title": window.title[:80]})
        return VerifiedEffect(
            expected=f"janela visível de {process_stem}",
            observed=f"{len(matches)} janela(s)",
            source="win32", verified=bool(matches),
            confidence=1.0 if matches else 0.6,
            details={"matches": matches[:5]},
        )

    def verify_file(self, path: str, *, expect_exists: bool = True,
                    content_contains: str | None = None) -> VerifiedEffect:
        from pathlib import Path

        target = Path(path)
        exists = target.is_file()
        observed = f"exists={exists}"
        verified: bool | None
        if expect_exists and not exists:
            verified = False
        else:
            verified = exists == expect_exists
        details: dict[str, Any] = {"path": str(target)}
        if exists and content_contains:
            try:
                content = target.read_text(encoding="utf-8", errors="ignore")
                hit = content_contains in content
                observed += f"; content_match={hit}"
                verified = bool(verified and hit)
                details["content_length"] = len(content)
            except OSError as error:
                observed += f"; read_failed={type(error).__name__}"
                verified = None
        elif exists:
            try:
                stat = target.stat()
                observed += f"; size={stat.st_size}; mtime={int(stat.st_mtime)}"
                details.update(size=stat.st_size, mtime=stat.st_mtime)
            except OSError:
                pass
        return VerifiedEffect(
            expected=f"exists={expect_exists}" +
                     (f"; contém conteúdo" if content_contains else ""),
            observed=observed, source="filesystem", verified=verified,
            confidence=1.0 if verified else 0.5, details=details,
        )

    def verify_process(self, pid: int | None = None, name: str | None = None, *,
                       expect_exists: bool = True) -> VerifiedEffect:
        import psutil

        found: dict[str, Any] | None = None
        if pid:
            try:
                proc = psutil.Process(pid)
                found = {"pid": pid, "name": proc.name()}
            except psutil.Error:
                found = None
        elif name:
            target = name.casefold().removesuffix(".exe")
            for proc in psutil.process_iter(["pid", "name"]):
                pname = (proc.info.get("name") or "").casefold().removesuffix(".exe")
                if pname == target:
                    found = {"pid": proc.info["pid"], "name": pname}
                    break
        verified = bool(found) == expect_exists
        return VerifiedEffect(
            expected=f"process {'existe' if expect_exists else 'não existe'}",
            observed=json_compact(found) or "not found", source="psutil",
            verified=verified, confidence=1.0 if found is not None else 0.7,
        )

    def verify_browser_tab(self, url_token: str, *,
                           tabs_fn: Callable[[], list[dict]] | None = None) -> VerifiedEffect:
        if tabs_fn is None:
            return VerifiedEffect(
                expected=f"tab {url_token}", observed="sem automação de browser ativa",
                source="browser", verified=None, confidence=0.2)
        try:
            tabs = tabs_fn() or []
        except Exception:  # noqa: BLE001
            tabs = []
        token = url_token.casefold()
        hit = next((tab for tab in tabs if token in str(tab.get("url", "")).casefold()
                    or token in str(tab.get("title", "")).casefold()), None)
        return VerifiedEffect(
            expected=f"tab correspondente a {url_token}",
            observed=(f"url={hit.get('url')}" if hit else f"{len(tabs)} tab(s) sem match"),
            source="browser", verified=bool(hit),
            confidence=1.0 if hit else 0.6,
        )

    @staticmethod
    def from_operation_result(payload: dict[str, Any], *,
                              expected: str = "") -> VerifiedEffect:
        """Mapeia operation_result do DesktopController para VerifiedEffect."""
        effect_verified = payload.get("effect_verified")
        verified = None if effect_verified is None else bool(effect_verified)
        detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
        windows = payload.get("windows") or detail.get("windows") or []
        observed_parts = [str(payload.get("message") or "")[:120]]
        if windows:
            first = windows[0]
            hwnd = first.get("hwnd")
            pid = first.get("pid")
            if hwnd:
                observed_parts.append(f"hwnd={hwnd}")
            if pid:
                observed_parts.append(f"pid={pid}")
        already = payload.get("already_open") or detail.get("already_open")
        if already:
            observed_parts.append("already_open=true")
        return VerifiedEffect(
            expected=expected or str(payload.get("action") or "operação"),
            observed="; ".join(part for part in observed_parts if part),
            source="operator+win32",
            verified=verified,
            confidence=1.0 if verified else (0.5 if verified is None else 0.3),
            details={"success": payload.get("success"),
                     "verification_status": payload.get("verification_status"),
                     "error_code": payload.get("error_code")},
        )


def json_compact(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""
