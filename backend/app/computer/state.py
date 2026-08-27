r"""Camada 2 — ComputerStateService (nyra-7c §17-§22).

Representação compacta e ATUAL do estado operacional com freshness por slot
(FRESH/STALE/UNKNOWN, §19), contexto de referência natural ("ele", "isso",
"a pasta", §18) isolado por conversa/turno (§20), atividade do usuário
(ACTIVE/IDLE/AWAY, §21) e integração lazy com o World State existente (§22).

Persistência de shutdown é atômica e mínima (§83): apenas contexto útil,
nunca conteúdo privado nem ações incompletas como sucesso.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("nyra.computer.state")


class Freshness(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


@dataclass
class StateSlot:
    value: Any
    source: str
    observed_at: float
    ttl_seconds: float = 8.0
    stale_after_seconds: float = 45.0
    confidence: float = 1.0

    def freshness(self, now: float | None = None) -> Freshness:
        reference = time.time() if now is None else now
        age = max(0.0, reference - self.observed_at)
        if age <= self.ttl_seconds:
            return Freshness.FRESH
        if age <= self.stale_after_seconds:
            return Freshness.STALE
        return Freshness.UNKNOWN


@dataclass
class ResolvedTarget:
    """Resultado da resolução de uma referência contextual (§18)."""

    kind: str  # app | window | folder | file | browser
    display_name: str
    process_names: tuple[str, ...] = ()
    title_tokens: tuple[str, ...] = ()
    path: str | None = None
    source_slot: str = ""
    freshness: Freshness = Freshness.FRESH


def _default_base() -> Path:
    override = os.environ.get("NYRA_COMPUTER_STATE_HOME")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "NYRA" / "computer-state"


_KIND_BY_TOKEN = {
    "a janela": "window", "essa janela": "window", "essa aqui": "window",
    "o arquivo": "file", "esse arquivo": "file", "este arquivo": "file",
    "a pasta": "folder", "essa pasta": "folder",
}


def _last_input_idle_seconds() -> float:
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)
    except Exception:  # noqa: BLE001
        return 0.0


class ComputerStateService:
    """Estado operacional compacto + resolução de referências (§17-§20)."""

    MAX_OVERLAYS = 256

    def __init__(
        self,
        perception=None,
        desktop=None,
        *,
        base_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
        idle_fn: Callable[[], float] | None = None,
    ) -> None:
        self.perception = perception
        self.desktop = desktop
        self.clock = clock
        self._idle_fn = idle_fn or _last_input_idle_seconds
        self.slots: dict[str, StateSlot] = {}
        self._overlays: dict[tuple[str, str], dict[str, dict]] = {}
        self._conversation_targets: dict[str, dict[str, dict]] = {}
        self.base_dir = base_dir or _default_base()
        self.context_path = self.base_dir / "context.json"

    # ---------------------------------------------------------------- slots

    def update(self, key: str, value: Any, *, source: str, ttl_seconds: float = 8.0,
               confidence: float = 1.0, stale_after_seconds: float = 45.0) -> None:
        self.slots[key] = StateSlot(
            value=value, source=source, observed_at=self.clock(),
            ttl_seconds=ttl_seconds, confidence=confidence,
            stale_after_seconds=stale_after_seconds,
        )

    def get(self, key: str) -> tuple[Any, Freshness]:
        slot = self.slots.get(key)
        if slot is None:
            return None, Freshness.UNKNOWN
        return slot.value, slot.freshness(self.clock())

    # ------------------------------------------------------- perception sync

    def refresh_from_perception(self, snapshot: dict[str, Any]) -> None:
        """Alimenta slots deriváveis a partir de um snapshot (§17)."""
        fg = snapshot.get("foreground_window")
        previous_fg, _ = self.get("foreground_window")
        if fg:
            self.update("foreground_window", fg, source="win32", ttl_seconds=4.0)
            self.update("foreground_app", fg.get("process") or "unknown",
                        source="win32", ttl_seconds=4.0)
            if previous_fg and isinstance(previous_fg, dict) and \
                    previous_fg.get("hwnd") != fg.get("hwnd"):
                self.update("last_foreground_window", previous_fg, source="win32",
                            ttl_seconds=600, stale_after_seconds=3600)
        windows = snapshot.get("windows") or []
        apps = sorted({w["process"] for w in windows if w.get("process")})
        self.update("open_apps", apps, source="win32", ttl_seconds=6.0)
        previous_recent, _ = self.get("recent_apps")
        recent = [str(item) for item in (previous_recent or []) if item in apps]
        foreground_app = str((fg or {}).get("process") or "")
        if foreground_app:
            recent = [foreground_app, *[item for item in recent if item != foreground_app]]
        self.update("recent_apps", recent[:12], source="win32", ttl_seconds=60.0,
                    stale_after_seconds=600.0)
        clipboard = snapshot.get("clipboard")
        if clipboard is not None:
            self.update("clipboard_metadata", clipboard, source="user32", ttl_seconds=10.0)
        recent_files = snapshot.get("recent_files") or []
        if recent_files:
            self.update("last_opened_file", recent_files[0], source="filesystem",
                        ttl_seconds=30.0, stale_after_seconds=300.0)
        for source_key, slot_key, source in (
            ("browser", "browser_context", "browser"),
            ("homelab", "homelab_summary", "world_state"),
            ("network", "network_summary", "world_state"),
        ):
            value = snapshot.get(source_key)
            if value is not None:
                self.update(slot_key, value, source=source, ttl_seconds=10.0,
                            stale_after_seconds=60.0)
        self.update("user_activity_state", self.user_activity(), source="win32_idle",
                    ttl_seconds=5.0, stale_after_seconds=30.0)

    def user_activity(self) -> str:
        idle = self._idle_fn()
        if idle < 60:
            return "ACTIVE"
        if idle < 300:
            return "IDLE"
        return "AWAY"

    # ------------------------------------------------- contexto de ações §18

    def note_action(self, *, action: str, kind: str, display_name: str,
                    verified: bool, conversation_id: str = "default",
                    turn_id: str | None = None, process_names: tuple[str, ...] = (),
                    title_tokens: tuple[str, ...] = (), path: str | None = None) -> None:
        """Registra alvo da última ação para pronomes e 'de novo' (§18/§20)."""
        target: dict[str, Any] = {
            "action": action,
            "kind": kind,
            "display_name": display_name,
            "verified": bool(verified),
            "observed_at": self.clock(),
        }
        if process_names:
            target["process_names"] = list(process_names)
        if title_tokens:
            target["title_tokens"] = list(title_tokens)
        if path:
            target["path"] = path
        self.update("last_action", target, source="operator", ttl_seconds=1800,
                    stale_after_seconds=7200)
        if not verified:
            # A tentativa permanece observável, mas não vira alvo contextual:
            # pronomes nunca apontam para um efeito que não foi comprovado.
            return
        self.update("last_target", target, source="operator", ttl_seconds=1800,
                    stale_after_seconds=7200)
        kind_slot = f"last_target_{kind}"
        self.update(kind_slot, target, source="operator", ttl_seconds=1800,
                    stale_after_seconds=7200)
        self.update("last_successful_action", target, source="operator",
                    ttl_seconds=1800, stale_after_seconds=7200)
        self.update("last_verified_action", target, source="operator",
                    ttl_seconds=1800, stale_after_seconds=7200)
        conversation_key = conversation_id or "default"
        conversation = self._conversation_targets.setdefault(conversation_key, {})
        conversation["last_target"] = target
        conversation[kind_slot] = target
        if len(self._conversation_targets) > self.MAX_OVERLAYS:
            self._conversation_targets.pop(next(iter(self._conversation_targets)), None)
        if turn_id:
            overlay = self._overlay(conversation_id, turn_id)
            overlay["last_target"] = target
            overlay[kind_slot] = target

    def _overlay(self, conversation_id: str, turn_id: str) -> dict[str, dict]:
        key = (conversation_id or "default", turn_id or "")
        if key not in self._overlays:
            if len(self._overlays) >= self.MAX_OVERLAYS:
                for stale in list(self._overlays)[:64]:
                    self._overlays.pop(stale, None)
            self._overlays[key] = {}
        return self._overlays[key]

    # ------------------------------------------------ referências (§28)

    def resolve_reference(self, token: str, *, conversation_id: str = "default",
                          turn_id: str | None = None) -> ResolvedTarget | None:
        lowered = " ".join((token or "").casefold().split())
        wanted_kind = _KIND_BY_TOKEN.get(lowered)
        candidates: list[dict[str, Any]] = []
        if turn_id:
            overlay = self._overlay(conversation_id, turn_id)
            if wanted_kind and f"last_target_{wanted_kind}" in overlay:
                candidates.append(overlay[f"last_target_{wanted_kind}"])
            elif "last_target" in overlay:
                candidates.append(overlay["last_target"])
        conversation = self._conversation_targets.get(conversation_id or "default", {})
        if not candidates and conversation:
            if wanted_kind and f"last_target_{wanted_kind}" in conversation:
                candidates.append(conversation[f"last_target_{wanted_kind}"])
            elif "last_target" in conversation:
                candidates.append(conversation["last_target"])
        if not candidates:
            # O contexto global persistido só é fallback da conversa default;
            # uma conversa nova nunca herda silenciosamente o alvo de outra.
            if (conversation_id or "default") != "default":
                return self._foreground_reference(wanted_kind)
            if wanted_kind:
                value, fresh = self.get(f"last_target_{wanted_kind}")
                if value:
                    candidates.append(value)
            else:
                value, fresh = self.get("last_target")
                if value:
                    candidates.append(value)
        for target in candidates:
            resolved = self._target_to_resolved(target)
            if resolved is not None:
                return resolved
        return self._foreground_reference(wanted_kind)

    def _foreground_reference(self, wanted_kind: str | None) -> ResolvedTarget | None:
        if wanted_kind in {"file", "folder", "browser"}:
            return None
        foreground, freshness = self.get("foreground_window")
        if not isinstance(foreground, dict) or freshness == Freshness.UNKNOWN:
            return None
        title = str(foreground.get("title") or "").strip()
        process = str(foreground.get("process") or "").casefold().removesuffix(".exe")
        name = title or process
        if not name:
            return None
        return ResolvedTarget(
            kind="window", display_name=name,
            process_names=(process,) if process else (),
            title_tokens=(title.casefold(),) if title else (),
            source_slot="foreground_window", freshness=freshness,
        )

    def _target_to_resolved(self, target: dict[str, Any]) -> ResolvedTarget | None:
        name = str(target.get("display_name") or "").strip()
        if not name:
            return None
        age = max(0.0, self.clock() - float(target.get("observed_at") or 0))
        freshness = Freshness.FRESH if age <= 60 else (
            Freshness.STALE if age <= 7200 else Freshness.UNKNOWN)
        return ResolvedTarget(
            kind=str(target.get("kind") or "app"),
            display_name=name,
            process_names=tuple(target.get("process_names") or ()),
            title_tokens=tuple(target.get("title_tokens") or ()) or (name.casefold(),),
            path=target.get("path"),
            source_slot="last_target",
            freshness=freshness,
        )

    # ------------------------------------------------------ world/persistência

    def world_summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for key in ("foreground_app", "open_apps", "clipboard_metadata"):
            value, fresh = self.get(key)
            summary[key] = {"value": value, "freshness": fresh.value}
        try:
            from app.core.release_info import world_state_snapshot

            summary["world_state_keys"] = sorted(world_state_snapshot().keys())
        except Exception:  # noqa: BLE001
            summary["world_state_keys"] = []
        return summary

    def save_context(self) -> bool:
        """Persistência atômica do contexto mínimo (§83)."""
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "saved_at": self.clock(), "slots": {}}
            for key in ("last_target", "last_successful_action"):
                slot = self.slots.get(key)
                if slot is not None:
                    payload["slots"][key] = {
                        "value": slot.value, "source": slot.source,
                        "observed_at": slot.observed_at,
                    }
            for key, slot in self.slots.items():
                if key.startswith("last_target_") and key not in payload["slots"]:
                    payload["slots"][key] = {
                        "value": slot.value, "source": slot.source,
                        "observed_at": slot.observed_at,
                    }
            tmp = self.context_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.context_path)
            return True
        except OSError as error:
            logger.warning("computer_state_save_failed type=%s", type(error).__name__)
            return False

    def load_context(self) -> bool:
        try:
            if not self.context_path.is_file():
                return False
            raw = json.loads(self.context_path.read_text(encoding="utf-8"))
            for key, item in (raw.get("slots") or {}).items():
                self.slots[key] = StateSlot(
                    value=item.get("value"), source=str(item.get("source") or "disk"),
                    observed_at=float(item.get("observed_at") or 0.0),
                    ttl_seconds=1.0,  # vira STALE imediatamente: revalidar antes de usar
                )
            return True
        except (OSError, ValueError):
            return False
