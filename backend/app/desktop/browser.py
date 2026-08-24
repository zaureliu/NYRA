"""Browser control adapter via Chrome DevTools Protocol (spec §71-§81).

NYRA manages its own browser instance with a dedicated profile so the
--remote-debugging-port flag reliably applies; attaching to an arbitrary
already-running browser is not attempted (the OS reuses the existing process
and silently drops the flag). When CDP is unavailable the tools answer
honestly with CAPABILITY_UNAVAILABLE instead of pretending.

No cookies/tokens/storage ever leave these calls (§80): only URLs and titles
are returned to the LLM.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import time
from pathlib import Path as _Path
from typing import Any

import httpx

from app.desktop.control import operation_result

logger = logging.getLogger("nyra.desktop.browser")

_CDP_BASE_PORT = 9333
_CDP_PORT_RANGE = 20
_BROWSER_SPAWN_TIMEOUT = 12.0

_MANAGED_PROFILE_ROOT = None


class BrowserManager:
    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self.port: int | None = None
        self.browser_name: str | None = None

    def status(self) -> dict:
        alive = self._process is not None and self._process.poll() is None and self.port is not None
        reachable = False
        version = ""
        if self.port:
            ok, payload = _http_json(self.port, "/json/version", timeout=1.5)
            reachable = ok
            version = str((payload or {}).get("Browser", ""))[:60]
        return {"managed": bool(alive), "reachable": reachable, "browser": self.browser_name,
                "port": self.port, "version": version}

    async def ensure_running(self, browser: str = "") -> tuple[bool, dict]:
        """Start a managed Chrome/Edge with CDP; returns (ok, info)."""
        if self.status()["reachable"]:
            return True, {"port": self.port, "browser": self.browser_name}
        executable = _find_browser_executable(browser)
        if executable is None:
            return False, {"error_code": "CAPABILITY_UNAVAILABLE",
                           "message": "Nenhum Chrome/Edge encontrado para controle CDP."}
        from app.core.paths import DATA_ROOT
        from pathlib import Path as _Path

        profile_root = DATA_ROOT / "browser-profile"
        stem = _Path(executable).stem
        # Adota um CDP já vivo de uma execução anterior antes de lançar outro.
        for offset in range(_CDP_PORT_RANGE):
            candidate_port = _CDP_BASE_PORT + offset
            ok, payload = _http_json(candidate_port, "/json/version", timeout=0.6)
            if ok:
                self.port = candidate_port
                self.browser_name = str((payload or {}).get("Browser", ""))[:40] or stem
                return True, {"port": candidate_port, "browser": self.browser_name, "adopted": True}
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen(  # noqa: S603 - executável descoberto em caminhos padrão do sistema
            [
                executable,
                f"--remote-debugging-port={_CDP_BASE_PORT}",
                f"--user-data-dir={profile_root / stem}",
                "--no-first-run", "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "about:blank",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=creationflags,
        )
        deadline = time.monotonic() + _BROWSER_SPAWN_TIMEOUT
        while time.monotonic() < deadline:
            ok, payload = _http_json(_CDP_BASE_PORT, "/json/version", timeout=1.0)
            if ok:
                self._process = process
                self.port = _CDP_BASE_PORT
                self.browser_name = stem
                return True, {"port": _CDP_BASE_PORT, "browser": stem}
            time.sleep(0.4)
        return False, {"error_code": "CDP_UNAVAILABLE", "message": "Não consegui abrir o navegador com porta de depuração livre."}

    def shutdown(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()


def _find_browser_executable(browser: str = "") -> str | None:
    candidates: list[str] = []
    preferred = browser.strip().casefold()
    order = ["chrome", "msedge"] if preferred != "edge" else ["msedge", "chrome"]
    if preferred == "firefox":
        order = []
    for name in order:
        program_files = os_environ_paths()
        for base in program_files:
            sub = "Google/Chrome/Application/chrome.exe" if name == "chrome" else "Microsoft/Edge/Application/msedge.exe"
            candidates.append(str(base / sub))
    for candidate in candidates:
        if _Path(candidate).is_file():
            return candidate
    import shutil

    return shutil.which("chrome") or shutil.which("msedge")


def os_environ_paths() -> list:
    from pathlib import Path

    roots = []
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        value = None
        import os

        value = os.environ.get(var)
        if value:
            roots.append(Path(value))
    return roots


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _http_json(port: int, path: str, timeout: float = 3.0, method: str = "GET") -> tuple[bool, dict | list | None]:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        response = httpx.request(method, url, timeout=timeout)
        if response.status_code >= 400:
            return False, None
        text = response.text.strip()
        if not text:
            return True, None
        return True, json.loads(text)
    except (httpx.HTTPError, ValueError, OSError):
        return False, None


def _ws_command(port: int, target_id: str, method: str, params: dict | None = None, timeout: float = 6.0) -> tuple[bool, dict | None]:
    """Single-shot CDP command over the DevTools websocket of one target."""
    try:
        import websocket  # websocket-client, já usado pelos smokes E2E
    except ImportError:
        return False, None
    url = f"ws://127.0.0.1:{port}/devtools/page/{target_id}"
    try:
        connection = websocket.WebSocket()
        connection.settimeout(timeout)
        connection.connect(url, http_proxy_host=None, suppress_origin=True)
        connection.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = connection.recv()
            message = json.loads(raw)
            if message.get("id") == 1:
                connection.close()
                return "error" not in message, message.get("result")
        connection.close()
        return False, None
    except (OSError, ValueError):
        return False, None


def _tabs(port: int) -> list[dict]:
    ok, payload = _http_json(port, "/json/list")
    if not ok or not isinstance(payload, list):
        return []
    return [
        {"id": item.get("id"), "title": (item.get("title") or "")[:120], "url": (item.get("url") or "")[:300],
         "type": item.get("type")}
        for item in payload if item.get("type") == "page"
    ]


class BrowserController:
    def __init__(self) -> None:
        self.manager = BrowserManager()

    async def open(self, url: str, browser: str = "") -> dict:
        clean = url.strip()
        if not clean.casefold().startswith(("http://", "https://")):
            return operation_result(app="browser", action="open", success=False,
                                    error_code="INVALID_URL", message="Somente http/https são aceitos aqui.",
                                    execution_success=False)
        started = time.perf_counter()
        running, info = await self.manager.ensure_running(browser)
        if not running:
            return operation_result(app="browser", action="open", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code=info.get("error_code", "CDP_UNAVAILABLE"),
                                    message=info.get("message", "Navegador CDP indisponível."), execution_success=False)
        port = int(info["port"])
        encoded = clean.replace(" ", "%20")
        ok, payload = _http_json(port, f"/json/new?{encoded}", method="PUT")
        tabs = _tabs(port)
        created = next((tab for tab in tabs if clean[:120] in (tab["url"] or "")), None)
        verified = bool(ok) and created is not None
        return operation_result(app="browser", action="open", duration_ms=(time.perf_counter() - started) * 1000,
                                success=bool(ok), execution_success=bool(ok),
                                effect_verified=verified,
                                verification_status="VERIFIED" if verified else "EXECUTED",
                                message=f"Aba aberta: {created['title'][:80]}" if created else "Comando de nova aba aceito.",
                                detail={"browser": info.get("browser"), "tabs": tabs[:8]})

    async def navigate(self, url: str, tab_id: str = "") -> dict:
        started = time.perf_counter()
        clean = url.strip()
        if not clean.casefold().startswith(("http://", "https://")):
            return operation_result(app="browser", action="navigate", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="INVALID_URL", message="URL deve ser http(s).",
                                    execution_success=False)
        running, info = await self.manager.ensure_running()
        if not running:
            return operation_result(app="browser", action="navigate", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code=info.get("error_code", "CDP_UNAVAILABLE"),
                                    message=info.get("message", "Navegador CDP indisponível."), execution_success=False)
        port = int(info["port"])
        target_id = tab_id.strip() or (_tabs(port)[0]["id"] if _tabs(port) else "")
        if not target_id:
            return operation_result(app="browser", action="navigate", duration_ms=(time.perf_counter() - started) * 1000,
                                    success=False, error_code="WINDOW_NOT_FOUND", message="Nenhuma aba aberta.",
                                    execution_success=False)
        ok, _ = await _to_thread(_ws_command, port, target_id, "Page.navigate", {"url": clean})
        await _sleep(1.2)
        tabs = _tabs(port)
        current = next((tab for tab in tabs if tab["id"] == target_id), None)
        verified = bool(ok) and current is not None and clean[:100] in (current["url"] or "")
        return operation_result(app="browser", action="navigate", duration_ms=(time.perf_counter() - started) * 1000,
                                success=bool(ok), execution_success=bool(ok), effect_verified=verified,
                                verification_status="VERIFIED" if verified else ("EXECUTED" if ok else "EXECUTION_FAILED"),
                                message=f"Navegado para {clean[:90]}.",
                                detail={"tab": current})

    async def tabs(self) -> dict:
        await self.manager.ensure_running()
        port = self.manager.port
        if not port or not self.manager.status()["reachable"]:
            return operation_result(app="browser", action="tabs", success=False,
                                    error_code="CAPABILITY_UNAVAILABLE",
                                    message="Nenhum navegador gerenciado em execução.", execution_success=False)
        listing = _tabs(port)
        return operation_result(app="browser", action="tabs", success=True, effect_verified=True,
                                verification_status="VERIFIED",
                                detail={"count": len(listing), "tabs": listing})

    async def close_tab(self, tab_id: str) -> dict:
        running, _info = await self.manager.ensure_running()
        if running:
            port = self.manager.port
        else:
            port = None
        if not port or not self.manager.status()["reachable"]:
            return operation_result(app="browser", action="close_tab", success=False,
                                    error_code="CAPABILITY_UNAVAILABLE", message="Navegador não gerenciado.",
                                    execution_success=False)
        before = {tab["id"] for tab in _tabs(port)}
        if tab_id.strip() not in before:
            return operation_result(app="browser", action="close_tab", success=False,
                                    error_code="UI_ELEMENT_NOT_FOUND", message="Aba inexistente.", execution_success=False)
        ok, _ = _http_json(port, f"/json/close/{tab_id.strip()}", method="PUT")
        await _sleep(0.6)
        after = {tab["id"] for tab in _tabs(port)}
        verified = tab_id.strip() not in after
        return operation_result(app="browser", action="close_tab", success=bool(ok), execution_success=bool(ok),
                                effect_verified=verified,
                                verification_status="VERIFIED" if verified else "VERIFICATION_FAILED",
                                message="Aba fechada e confirmada." if verified else "Ainda presente após comando.")

    async def page_command(self, action: str) -> dict:
        """refresh/back/forward on the active tab with URL verification."""
        await self.manager.ensure_running()
        port = self.manager.port
        method_map = {
            "refresh": ("Page.reload", {"ignoreCache": False}),
            "back": ("Page.navigateToHistoryEntry", None),
            "forward": ("Page.navigateToHistoryEntry", None),
        }
        if action not in method_map:
            return operation_result(app="browser", action=action, success=False,
                                    error_code="INVALID_ACTION", message="Use refresh/back/forward.")
        if not port or not self.manager.status()["reachable"]:
            return operation_result(app="browser", action=action, success=False,
                                    error_code="CAPABILITY_UNAVAILABLE", message="Navegador não gerenciado.",
                                    execution_success=False)
        tabs = _tabs(port)
        if not tabs:
            return operation_result(app="browser", action=action, success=False,
                                    error_code="WINDOW_NOT_FOUND", message="Sem abas.", execution_success=False)
        target = tabs[0]
        if action == "refresh":
            ok, _ = await _to_thread(_ws_command, port, target["id"], "Page.reload", {"ignoreCache": False})
            verified = bool(ok)
        else:
            delta = -1 if action == "back" else 1
            ok_history, history = await _to_thread(_ws_command, port, target["id"], "Page.getNavigationHistory", {})
            entries = ((history or {}).get("entries") or [])
            index = int((history or {}).get("currentIndex", 0)) + delta
            if not ok_history or not (0 <= index < len(entries)):
                return operation_result(app="browser", action=action, success=False,
                                        error_code="UI_ACTION_FAILED",
                                        message="Sem histórico nessa direção.", execution_success=False)
            entry_id = entries[index].get("id")
            ok, _ = await _to_thread(_ws_command, port, target["id"], "Page.navigateToHistoryEntry", {"entryId": entry_id})
            await _sleep(1.0)
            current_url = (_tabs(port)[0]["url"] if _tabs(port) else "")
            verified = bool(ok) and entries[index].get("url", "")[:100] in current_url
        return operation_result(app="browser", action=action, success=bool(ok), execution_success=bool(ok),
                                effect_verified=verified,
                                verification_status="VERIFIED" if verified else "EXECUTED" if ok else "EXECUTION_FAILED",
                                message=f"{action} aplicado na aba ativa.")


async def _to_thread(fn, *args, **kwargs):
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
