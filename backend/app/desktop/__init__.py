"""KAZUMI Desktop Application Control V1."""

from app.desktop.apps import DesktopAppsRegistry, load_desktop_apps
from app.desktop.control import DesktopController
from app.desktop.models import DesktopAppSpec, LaunchErrorCode, WindowInfo
from app.desktop.tools import register_desktop_tools

__all__ = [
    "DesktopAppSpec",
    "DesktopAppsRegistry",
    "DesktopController",
    "LaunchErrorCode",
    "WindowInfo",
    "load_desktop_apps",
    "register_desktop_tools",
]
