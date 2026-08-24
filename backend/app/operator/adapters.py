"""App Adapter Framework (spec Parte B §35-§45).

Generic desktop control keeps working for every app; adapters exist only where
they improve reliability (§39). Interface (§37): detect/launch/status/
capabilities/inspect/execute_action/verify. Registry resolves by app id.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from app.desktop.control import operation_result


def _find_exe(candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


class AppAdapter(ABC):
    """One application-specific reliability layer."""

    app_id: str = "base"
    display_name: str = "Base"

    @abstractmethod
    def detect(self) -> bool: ...

    @abstractmethod
    def capabilities(self) -> list[str]: ...

    @abstractmethod
    async def status(self) -> dict: ...

    async def launch(self, params: dict[str, Any]) -> dict:
        raise NotImplementedError

    async def execute_action(self, action: str, params: dict[str, Any]) -> dict:
        return operation_result(app=self.app_id, action=action, success=False,
                                error_code="UNSUPPORTED_ACTION",
                                message=f"'{action}' não é suportado pelo adapter {self.app_id}.")

    def _spawn(self, argv: list[str], confirm_seconds: float = 6.0) -> dict:
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        started = time.perf_counter()
        try:
            process = subprocess.Popen(  # noqa: S603 - executável detectado em caminho fixo confiável
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, creationflags=creationflags,
            )
        except OSError as exc:
            return {"success": False, "error_code": "SPAWN_FAILED", "message": str(exc)}
        deadline = time.monotonic() + max(1.0, min(confirm_seconds, 12.0))
        while time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.25)
        return {
            "success": True, "pid": process.pid,
            "still_running": process.poll() is None,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    @staticmethod
    def _count_processes(prefixes: tuple[str, ...]) -> int:
        import psutil

        return sum(
            1 for process in psutil.process_iter(["name"])
            if (process.info.get("name") or "").casefold().startswith(prefixes)
        )


# ---------------------------------------------------------------------- vscode
class VSCodeAdapter(AppAdapter):
    """CLI-first (§41): workspace/file ops via `code`; UIA stays fallback."""

    app_id = "vscode"
    display_name = "VS Code"

    def __init__(self) -> None:
        self._cli_cache: str | None | bool = None

    def _code_cli(self) -> str | None:
        if isinstance(self._cli_cache, str):
            return self._cli_cache or None
        if self._cli_cache is False:
            return None
        resolved = _find_exe([
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
            r"C:\Program Files\Microsoft VS Code\Code.exe",
            r"C:\Program Files (x86)\Microsoft VS Code\Code.exe",
        ]) or shutil.which("code")
        self._cli_cache = resolved or False
        return resolved

    def detect(self) -> bool:
        return self._code_cli() is not None

    def capabilities(self) -> list[str]:
        return ["status", "open_workspace", "open_file", "install_extension"] if self.detect() else []

    async def status(self) -> dict:
        instances = self._count_processes(("code",)) if self.detect() else 0
        return {"success": True, "adapter": self.app_id, "detected": self.detect(),
                "running_instances": instances, "capabilities": self.capabilities()}

    async def launch(self, params: dict[str, Any]) -> dict:
        cli = self._code_cli()
        if not cli:
            return operation_result(app=self.app_id, action="launch", success=False,
                                    error_code="CAPABILITY_UNAVAILABLE", message="VS Code não encontrado.")
        argv = [cli]
        if params.get("new_window"):
            argv.append("-n")
        target = str(params.get("path") or "").strip()
        if target:
            argv.append(target)
        outcome = await asyncio.to_thread(self._spawn, argv)
        verified = outcome["success"] and outcome.get("still_running", False)
        return operation_result(app=self.app_id, action="launch",
                                success=bool(outcome["success"]),
                                error_code=outcome.get("error_code"),
                                message=outcome.get("message", ""),
                                effect_verified=verified,
                                verification_status="VERIFIED" if verified else "EXECUTED",
                                detail={"pid": outcome.get("pid")})

    async def execute_action(self, action: str, params: dict[str, Any]) -> dict:
        if action in {"open_workspace", "open_file", "focus_file"}:
            path = str(params.get("path") or "").strip()
            if not path or not Path(path).exists():
                return operation_result(app=self.app_id, action=action, success=False,
                                        error_code="PATH_NOT_FOUND", message=f"Caminho inexistente: {path}")
            return await self.launch({"path": path, "new_window": action == "open_workspace"})
        if action == "install_extension":
            return await self._install_extension(str(params.get("extension") or ""))
        return await super().execute_action(action, params)

    async def _install_extension(self, extension: str) -> dict:
        cli = self._code_cli()
        if not cli or not extension.strip():
            return operation_result(app=self.app_id, action="install_extension", success=False,
                                    error_code="INVALID_PARAMS")
        completed = await asyncio.to_thread(
            lambda: subprocess.run(  # noqa: S603
                [cli, "--install-extension", extension.strip(), "--force"],
                capture_output=True, timeout=90,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        )
        ok = completed.returncode == 0
        stdout = completed.stdout.decode("utf-8", errors="replace")
        verified = ok and extension.casefold() in stdout.casefold()
        return operation_result(app=self.app_id, action="install_extension", success=ok,
                                effect_verified=verified,
                                verification_status="VERIFIED" if verified else ("EXECUTED" if ok else "EXECUTION_FAILED"),
                                detail={"stdout_tail": stdout[-300:]})

# -------------------------------------------------------------------- explorer
class ExplorerAdapter(AppAdapter):
    app_id = "explorer"
    display_name = "Windows Explorer"

    def detect(self) -> bool:
        return Path(os.environ.get("WINDIR", r"C:\Windows"), "explorer.exe").is_file()

    def capabilities(self) -> list[str]:
        return ["status", "open_folder", "select_file"] if self.detect() else []

    async def status(self) -> dict:
        instances = max(0, self._count_processes(("explorer",)) - 1)
        return {"success": True, "adapter": self.app_id, "detected": True,
                "running_instances": instances, "capabilities": self.capabilities(),
                "note": "O processo shell do Windows conta como uma instância."}

    async def launch(self, params: dict[str, Any]) -> dict:
        path = str(params.get("path") or "").strip() or os.path.expanduser("~")
        if not Path(path).exists():
            return operation_result(app=self.app_id, action="launch", success=False,
                                    error_code="PATH_NOT_FOUND", message=f"Pasta inexistente: {path}")
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        argv = [str(windir / "explorer.exe"), path]
        if params.get("select"):
            argv[-1] = f"/select,{params['select']}"
        outcome = await asyncio.to_thread(self._spawn, argv)
        verified = outcome["success"]
        return operation_result(app=self.app_id, action="launch",
                                success=bool(outcome["success"]),
                                error_code=outcome.get("error_code"),
                                effect_verified=verified,
                                verification_status="VERIFIED" if verified else "EXECUTION_FAILED",
                                detail={"pid": outcome.get("pid"), "path": path})

    async def execute_action(self, action: str, params: dict[str, Any]) -> dict:
        if action in {"open_folder", "navigate"}:
            return await self.launch({"path": params.get("path")})
        if action == "select_file":
            target = str(params.get("path") or "").strip()
            if not target or not Path(target).exists():
                return operation_result(app=self.app_id, action=action, success=False,
                                        error_code="PATH_NOT_FOUND")
            parent = str(Path(target).parent)
            outcome = await asyncio.to_thread(
                self._spawn,
                [str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe"), f"/select,{target}"],
            )
            return operation_result(app=self.app_id, action=action, success=bool(outcome["success"]),
                                    effect_verified=outcome.get("still_running", False),
                                    verification_status="VERIFIED",
                                    detail={"parent": parent})
        return await super().execute_action(action, params)


# ----------------------------------------------------------- windows terminal
class WindowsTerminalAdapter(AppAdapter):
    app_id = "windows_terminal"
    display_name = "Windows Terminal"

    def __init__(self) -> None:
        self._exe_cache: str | None | bool = None

    def _wt_exe(self) -> str | None:
        if self._exe_cache is False:
            return None
        if isinstance(self._exe_cache, str):
            return self._exe_cache
        local = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe")
        resolved = _find_exe([local]) or shutil.which("wt")
        self._exe_cache = resolved or False
        return resolved

    def detect(self) -> bool:
        return self._wt_exe() is not None

    def capabilities(self) -> list[str]:
        # §45: execução de comando real permanece no system_shell (schema/risk/
        # redaction/auditoria); o adapter só orquestra janelas/tabs.
        return ["status", "new_tab"] if self.detect() else []

    async def status(self) -> dict:
        instances = self._count_processes(("windowsterminal", "wt",)) if self.detect() else 0
        return {"success": True, "adapter": self.app_id, "detected": self.detect(),
                "running_instances": instances, "capabilities": self.capabilities()}

    async def launch(self, params: dict[str, Any]) -> dict:
        wt = self._wt_exe()
        if not wt:
            return operation_result(app=self.app_id, action="launch", success=False,
                                    error_code="CAPABILITY_UNAVAILABLE", message="wt.exe não encontrado.")
        argv = [wt]
        working_dir = str(params.get("working_directory") or "").strip()
        profile = str(params.get("profile") or "").strip()
        if params.get("new_tab", True) and params.get("_called_as") == "new_tab":
            argv.append("-w")
            argv.append("0")
            argv.append("new-tab")
        if profile:
            argv += ["-p", profile]
        if working_dir:
            argv += ["-d", working_dir]
        outcome = await asyncio.to_thread(self._spawn, argv)
        return operation_result(app=self.app_id, action=params.get("_called_as") or "launch",
                                success=bool(outcome["success"]),
                                error_code=outcome.get("error_code"),
                                effect_verified=outcome.get("still_running", False),
                                verification_status="VERIFIED" if outcome.get("still_running") else "EXECUTED",
                                detail={"pid": outcome.get("pid")})

    async def execute_action(self, action: str, params: dict[str, Any]) -> dict:
        if action in {"open", "new_tab"}:
            params = dict(params)
            params["_called_as"] = action
            return await self.launch(params)
        return await super().execute_action(action, params)


# -------------------------------------------------------------- chrome / edge
class ManagedBrowserAdapter(AppAdapter):
    """Wraps the existing CDP BrowserController (§40 Chrome/Edge; §47 preferir
    DevTools). No cookies/tokens cross this boundary."""

    def __init__(self, browser_controller, *, app_id: str, display_name: str) -> None:
        self.browser_controller = browser_controller
        self.app_id = app_id
        self.display_name = display_name

    def detect(self) -> bool:
        from app.desktop.browser import _find_browser_executable

        preferred = "edge" if self.app_id == "edge" else ""
        executable = _find_browser_executable(preferred)
        expected = "msedge" if self.app_id == "edge" else "chrome"
        return bool(executable) and expected in str(executable).casefold()

    def capabilities(self) -> list[str]:
        if not self.detect():
            return []
        return ["status", "open_url", "tabs", "navigate", "close_tab", "dom_inspect",
                "find_element", "click_element", "type_text", "wait_condition"]

    async def status(self) -> dict:
        managed = getattr(self.browser_controller.manager, "status", lambda: {})()
        detected = self.detect()
        return {"success": True, "adapter": self.app_id, "detected": detected,
                "managed_cdp": managed, "capabilities": self.capabilities()}

    async def launch(self, params: dict[str, Any]) -> dict:
        url = str(params.get("url") or "")
        if not url:
            return operation_result(app=self.app_id, action="launch", success=False,
                                    error_code="INVALID_URL", message="URL obrigatória.")
        return await self.browser_controller.open(url, browser="edge" if self.app_id == "edge" else "")

    async def execute_action(self, action: str, params: dict[str, Any]) -> dict:
        controller = self.browser_controller
        if action == "open_url":
            return await self.launch(params)
        if action == "tabs":
            return await controller.tabs()
        if action == "navigate":
            return await controller.navigate(str(params.get("url") or ""), str(params.get("tab_id") or ""))
        if action == "close_tab":
            return await controller.close_tab(str(params.get("tab_id") or ""))
        if hasattr(controller, "v2_dispatch"):
            return await controller.v2_dispatch(action, params)
        return await super().execute_action(action, params)


# --------------------------------------------------------------------- registry
class AdapterRegistry:
    """Resolves an AppAdapter by application id or fuzzy name (§38)."""

    def __init__(self, adapters: list[AppAdapter] | None = None) -> None:
        self._adapters: list[AppAdapter] = list(adapters or [])
        self._aliases: dict[str, str] = {
            "code": "vscode", "vs code": "vscode", "visual studio code": "vscode",
            "explorer": "explorer", "gerenciador de arquivos": "explorer", "files": "explorer",
            "terminal": "windows_terminal", "wt": "windows_terminal",
            "chrome": "chrome", "google chrome": "chrome",
            "edge": "edge", "microsoft edge": "edge",
        }

    def register(self, adapter: AppAdapter) -> None:
        self._adapters.append(adapter)

    def all_adapters(self) -> list[AppAdapter]:
        return list(self._adapters)

    def by_id(self, app_id: str) -> AppAdapter | None:
        wanted = self._aliases.get(app_id.strip().casefold(), app_id.strip().casefold())
        for adapter in self._adapters:
            if adapter.app_id == wanted:
                return adapter
        return None

    async def resolve(self, app_hint: str) -> dict:
        """Resolve + detect status for a hint like 'vscode' or 'abrir terminal'."""
        adapter = self.by_id(app_hint)
        if adapter is None:
            matches = [item for item in self._adapters
                       if item.display_name.casefold() in app_hint.casefold()]
            adapter = matches[0] if matches else None
        if adapter is None:
            return {"success": False, "error_code": "ADAPTER_NOT_FOUND",
                    "message": f"Nenhum adapter registrado para '{app_hint}'.",
                    "available": [item.app_id for item in self._adapters]}
        detected = await asyncio.to_thread(adapter.detect)
        return {"success": True, "app_id": adapter.app_id, "display_name": adapter.display_name,
                "detected": detected, "capabilities": adapter.capabilities()}


def create_adapter_registry(browser_controller=None) -> AdapterRegistry:
    adapters: list[AppAdapter] = [
        VSCodeAdapter(), ExplorerAdapter(), WindowsTerminalAdapter(),
    ]
    if browser_controller is not None:
        adapters.append(ManagedBrowserAdapter(browser_controller, app_id="chrome", display_name="Chrome"))
        adapters.append(ManagedBrowserAdapter(browser_controller, app_id="edge", display_name="Edge"))
    return AdapterRegistry(adapters)
