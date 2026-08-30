from __future__ import annotations

import re

import pytest

from app.desktop.models import operation_result
from app.desktop.presenter import ActionResultPresenter


@pytest.mark.parametrize(
    ("action", "app", "expected"),
    [
        ("launch_dynamic", "canva", "Canva aberto."),
        ("universal_close_app", "steam", "Steam fechado."),
        ("universal_minimize_app", "discord", "Discord minimizado."),
        ("universal_maximize_app", "discord", "Discord maximizado."),
        ("universal_restore_app", "discord", "Discord restaurado."),
        ("universal_focus_app", "chrome", "Chrome em primeiro plano."),
    ],
)
def test_normal_app_action_responses_are_clean(action: str, app: str, expected: str):
    result = operation_result(
        success=True,
        app=app,
        action=action,
        message="1 janela(s), pid=10184, hwnd=263606, foreground=true, com verificação.",
        execution_success=True,
        effect_verified=True,
        verification_status="VERIFIED",
        detail={
            "pid": 10184,
            "windows": [{"pid": 10184, "hwnd": 263606, "verified": True}],
            "launch_method": "ShellExecute",
            "attempts": [{"source": "Start Menu"}],
        },
    )

    assert result["user_facing_response"] == expected
    assert not re.search(
        r"(?i)\b(pid|hwnd|process|verification|foreground|shellexecute|start menu|janela\(s\))\b",
        result["user_facing_response"],
    )
    # Evidence is retained separately for diagnostics and explicit queries.
    assert result["pid"] == 10184
    assert result["windows"][0]["hwnd"] == 263606
    assert result["effect_verified"] is True
    assert "pid=10184" in result["message"]


def test_already_open_is_clean_and_keeps_focus_evidence_internal():
    result = operation_result(
        success=True,
        app="steam",
        action="launch_dynamic",
        message="Steam já estava aberto; janela existente em primeiro plano (pid 77).",
        execution_success=True,
        effect_verified=True,
        verification_status="VERIFIED",
        detail={
            "already_open": True,
            "foreground_expected": True,
            "windows": [{"pid": 77, "hwnd": 88, "verified": True}],
        },
    )

    assert result["user_facing_response"] == "Steam já estava aberto."
    assert result["foreground_expected"] is True
    assert result["windows"][0]["hwnd"] == 88


def test_failed_effect_never_becomes_false_success():
    result = operation_result(
        success=False,
        app="canva",
        action="launch_dynamic",
        message="Processo iniciado, mas nenhuma janela foi confirmada (pid 10184).",
        error_code="WINDOW_NOT_CONFIRMED",
        execution_success=True,
        effect_verified=False,
        verification_status="VERIFICATION_FAILED",
        detail={"pid": 10184},
    )

    assert result["user_facing_response"] == (
        "Não consegui confirmar que o Canva foi aberto."
    )
    assert result["effect_verified"] is False


def test_not_found_and_missing_executable_do_not_expose_discovery_details():
    not_found = operation_result(
        success=False,
        app="spotify",
        action="launch_dynamic",
        error_code="EXECUTABLE_NOT_FOUND",
        message="registry/path/start menu/aumid scan failed",
        execution_success=False,
        effect_verified=False,
        verification_status="NOT_EXECUTED",
    )
    unavailable = operation_result(
        success=False,
        app="spotify",
        action="launch_attempt",
        error_code="EXECUTABLE_NOT_FOUND",
        message=r"C:\internal\Spotify.exe not found",
        execution_success=False,
        effect_verified=False,
        verification_status="EXECUTION_FAILED",
        detail={"candidate": {"display_name": "Spotify", "target": r"C:\internal\Spotify.exe"}},
    )

    assert not_found["user_facing_response"] == "Não encontrei o aplicativo Spotify."
    assert unavailable["user_facing_response"] == (
        "Não consegui abrir o Spotify porque o executável não está disponível."
    )


def test_explicit_technical_view_can_use_retained_pid_and_hwnd():
    result = operation_result(
        success=True,
        app="canva",
        action="launch_dynamic",
        execution_success=True,
        effect_verified=True,
        verification_status="VERIFIED",
        detail={"pid": 10184, "windows": [{"pid": 10184, "hwnd": 263606}]},
    )

    normal = ActionResultPresenter.present(result)
    technical = ActionResultPresenter.present(result, include_technical=True)

    assert normal == "Canva aberto."
    assert technical is not None
    assert "PID: 10184" in technical
    assert "HWND: 263606" in technical


def test_restore_accepts_minimized_window_returning_to_maximized_state(monkeypatch):
    from app.desktop import window_manager

    class FakeUser32:
        iconic = True
        zoomed = True

        @staticmethod
        def GetAncestor(hwnd, _kind):
            return hwnd

        def IsIconic(self, _hwnd):
            return self.iconic

        def IsZoomed(self, _hwnd):
            return self.zoomed

        @staticmethod
        def IsWindowVisible(_hwnd):
            return True

        def ShowWindow(self, _hwnd, _command):
            self.iconic = False
            # Windows may preserve the pre-minimize maximized placement.
            self.zoomed = True
            return True

    monkeypatch.setattr(window_manager, "_user32", FakeUser32())

    assert window_manager.restore_window(123, timeout_seconds=0.01) is True
