"""User-facing presentation for structured desktop action results.

Execution and verification metadata remain in the result dictionary.  This
module owns the separate, intentionally small sentence shown in chat and sent
to speech synthesis.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


_DISPLAY_NAMES = {
    "bloco de notas": "Bloco de Notas",
    "bloco_de_notas": "Bloco de Notas",
    "canva": "Canva",
    "chrome": "Chrome",
    "code": "Visual Studio Code",
    "discord": "Discord",
    "google chrome": "Chrome",
    "notepad": "Bloco de Notas",
    "spotify": "Spotify",
    "steam": "Steam",
    "visual studio code": "Visual Studio Code",
    "visual_studio_code": "Visual Studio Code",
    "vscode": "Visual Studio Code",
}

_ACTION_ALIASES = {
    "close": "CLOSE_APP",
    "close_app": "CLOSE_APP",
    "focus": "FOCUS_APP",
    "focus_app": "FOCUS_APP",
    "launch": "OPEN_APP",
    "launch_attempt": "OPEN_APP",
    "launch_dynamic": "OPEN_APP",
    "maximize": "MAXIMIZE_APP",
    "maximize_app": "MAXIMIZE_APP",
    "minimize": "MINIMIZE_APP",
    "minimize_app": "MINIMIZE_APP",
    "open_app": "OPEN_APP",
    "restore": "RESTORE_APP",
    "restore_app": "RESTORE_APP",
    "switch": "SWITCH_APP",
    "switch_app": "SWITCH_APP",
}

_SUCCESS_MESSAGES = {
    "OPEN_APP": "{app} aberto.",
    "CLOSE_APP": "{app} fechado.",
    "MINIMIZE_APP": "{app} minimizado.",
    "MAXIMIZE_APP": "{app} maximizado.",
    "RESTORE_APP": "{app} restaurado.",
    "FOCUS_APP": "{app} em primeiro plano.",
    "SWITCH_APP": "{app} em primeiro plano.",
}

_VERIFICATION_FAILURE_MESSAGES = {
    "OPEN_APP": "Não consegui confirmar que o {app} foi aberto.",
    "CLOSE_APP": "Não consegui confirmar que o {app} foi fechado.",
    "MINIMIZE_APP": "Não consegui confirmar que o {app} foi minimizado.",
    "MAXIMIZE_APP": "Não consegui confirmar que o {app} foi maximizado.",
    "RESTORE_APP": "Não consegui confirmar que o {app} foi restaurado.",
    "FOCUS_APP": "Não consegui confirmar que o {app} ficou em primeiro plano.",
    "SWITCH_APP": "Não consegui confirmar que o {app} ficou em primeiro plano.",
}

_EXECUTION_FAILURE_MESSAGES = {
    "OPEN_APP": "Não consegui abrir o {app}.",
    "CLOSE_APP": "Não consegui fechar o {app}.",
    "MINIMIZE_APP": "Não consegui minimizar o {app}.",
    "MAXIMIZE_APP": "Não consegui maximizar o {app}.",
    "RESTORE_APP": "Não consegui restaurar o {app}.",
    "FOCUS_APP": "Não consegui trazer o {app} para primeiro plano.",
    "SWITCH_APP": "Não consegui trazer o {app} para primeiro plano.",
}

_NOT_FOUND_CODES = {"UNKNOWN_APP", "WINDOW_NOT_FOUND", "TARGET_NOT_FOUND"}
_VERIFICATION_FAILURE_CODES = {
    "CLOSE_APP_NOT_CONFIRMED",
    "CLOSE_NOT_CONFIRMED",
    "FOCUS_APP_NOT_CONFIRMED",
    "FOCUS_NOT_CONFIRMED",
    "MAXIMIZE_APP_NOT_CONFIRMED",
    "MAXIMIZE_NOT_CONFIRMED",
    "MINIMIZE_APP_NOT_CONFIRMED",
    "MINIMIZE_NOT_CONFIRMED",
    "RESTORE_APP_NOT_CONFIRMED",
    "RESTORE_NOT_CONFIRMED",
    "SWITCH_APP_NOT_CONFIRMED",
    "SWITCH_NOT_CONFIRMED",
    "WINDOW_NOT_CONFIRMED",
}


class ActionResultPresenter:
    """Convert internal desktop action results into concise operator text."""

    @classmethod
    def supports(cls, action: str | None) -> bool:
        return cls._action(action) in _SUCCESS_MESSAGES

    @classmethod
    def present(
        cls,
        result: Mapping[str, Any],
        *,
        requested_action: str | None = None,
        requested_app: str | None = None,
        include_technical: bool = False,
    ) -> str | None:
        if str(result.get("subject_kind") or "app").casefold() not in {
            "app", "window",
        }:
            return None
        action = cls._action(requested_action or str(result.get("action") or ""))
        if action not in _SUCCESS_MESSAGES:
            return None

        app = cls.display_name(result, requested_app=requested_app)
        error_code = str(result.get("error_code") or "").upper()
        status = str(result.get("verification_status") or "").upper()
        verified = result.get("effect_verified") is True
        success = result.get("success") is True

        if success and verified:
            if action == "OPEN_APP" and result.get("already_open"):
                response = f"{app} já estava aberto."
            else:
                response = _SUCCESS_MESSAGES[action].format(app=app)
        elif error_code == "AMBIGUOUS_APPLICATION":
            names = cls._option_names(result)
            response = (
                f"Encontrei mais de um aplicativo com esse nome: {' ou '.join(names)}. Qual deles?"
                if names else f"Encontrei mais de um aplicativo chamado {app}. Qual deles?"
            )
        elif error_code in _NOT_FOUND_CODES or (
            error_code == "EXECUTABLE_NOT_FOUND" and not result.get("candidate")
        ):
            response = f"Não encontrei o aplicativo {app}."
        elif action == "OPEN_APP" and error_code == "EXECUTABLE_NOT_FOUND":
            response = f"Não consegui abrir o {app} porque o executável não está disponível."
        elif (
            result.get("effect_verified") is False
            and (
                status == "VERIFICATION_FAILED"
                or error_code in _VERIFICATION_FAILURE_CODES
                or result.get("execution_success") is True
            )
        ):
            response = _VERIFICATION_FAILURE_MESSAGES[action].format(app=app)
        else:
            response = _EXECUTION_FAILURE_MESSAGES[action].format(app=app)

        if include_technical:
            details = cls.technical_details(result)
            if details:
                response = f"{response} {details}"
        return response

    @classmethod
    def display_name(
        cls,
        result: Mapping[str, Any],
        *,
        requested_app: str | None = None,
    ) -> str:
        candidate = result.get("candidate")
        candidate_name = (
            candidate.get("display_name")
            if isinstance(candidate, Mapping)
            else None
        )
        raw = str(
            candidate_name
            or result.get("display_name")
            or result.get("app")
            or requested_app
            or "aplicativo"
        ).strip().strip("\"'")
        raw = raw.removesuffix(".exe").replace("_", " ")
        # A presentation boundary must never turn a launch path into chat text.
        raw = re.split(r"[\\/]", raw)[-1].strip()
        known = _DISPLAY_NAMES.get(raw.casefold())
        if known:
            return known
        if not raw:
            return "aplicativo"
        if raw.islower() or raw.isupper():
            return " ".join(part.capitalize() for part in raw.split())
        return raw

    @staticmethod
    def technical_details(result: Mapping[str, Any]) -> str:
        """Expose retained identifiers only to an explicitly technical caller."""
        pids: list[int] = []
        hwnds: list[int] = []

        def add_unique(target: list[int], value: Any) -> None:
            if isinstance(value, int) and value > 0 and value not in target:
                target.append(value)

        add_unique(pids, result.get("pid") or result.get("process_id"))
        add_unique(hwnds, result.get("hwnd") or result.get("window_handle"))
        windows = result.get("windows")
        if isinstance(windows, list):
            for window in windows:
                if not isinstance(window, Mapping):
                    continue
                add_unique(pids, window.get("pid") or window.get("process_id"))
                add_unique(hwnds, window.get("hwnd") or window.get("window_handle"))

        parts: list[str] = []
        if pids:
            parts.append("PID: " + ", ".join(str(pid) for pid in pids))
        if hwnds:
            parts.append("HWND: " + ", ".join(str(hwnd) for hwnd in hwnds))
        if isinstance(windows, list):
            parts.append(f"janelas: {len(windows)}")
        if result.get("effect_verified") is not None:
            parts.append(
                "effect_verified: "
                + ("true" if result.get("effect_verified") is True else "false")
            )
        return "; ".join(parts) + ("." if parts else "")

    @staticmethod
    def _action(value: str | None) -> str:
        normalized = str(value or "").strip().casefold()
        if normalized.startswith("universal_"):
            normalized = normalized.removeprefix("universal_")
        if normalized.upper() in _SUCCESS_MESSAGES:
            return normalized.upper()
        return _ACTION_ALIASES.get(normalized, normalized.upper())

    @classmethod
    def _option_names(cls, result: Mapping[str, Any]) -> list[str]:
        options = result.get("options")
        if not isinstance(options, list):
            return []
        names: list[str] = []
        for option in options[:4]:
            if not isinstance(option, Mapping) or not option.get("display_name"):
                continue
            name = cls.display_name({"display_name": option["display_name"]})
            if name not in names:
                names.append(name)
        return names


def attach_user_facing_response(
    result: dict[str, Any],
    *,
    requested_action: str | None = None,
    requested_app: str | None = None,
) -> dict[str, Any]:
    """Attach the clean view without changing any internal result fields."""
    response = ActionResultPresenter.present(
        result,
        requested_action=requested_action,
        requested_app=requested_app,
    )
    if response:
        result["user_facing_response"] = response
    return result
