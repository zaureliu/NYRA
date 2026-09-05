"""Persistent, event-driven and grounded operational state for KAZUMI.

The engine owns no discovery mechanism.  It consumes already verified local
authorities and exposes a small stable API to Context/Operator/UI consumers.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import json
import os
from pathlib import Path
import re
from threading import RLock
import time
from typing import Any, Callable, Iterable

from app.events import Event, EventBus, EventType
from app.world_state.models import WorldEvent, WorldFreshness, WorldSnapshot, WorldValue


Clock = Callable[[], float]


@dataclass
class _Record:
    value: Any
    source: str
    observed_at: float
    confidence: float
    verified: bool
    ttl_seconds: float | None
    stale_after_seconds: float | None
    persist: bool


_SLOT_NAMES = (
    "current_app", "current_window", "current_process", "current_desktop_target",
    "current_browser", "current_url", "current_tab", "current_project",
    "current_file", "recent_files", "current_task", "current_operation",
    "recent_apps", "recent_artifacts", "active_monitors", "active_tasks",
    "active_goal", "open_loop_count", "waiting_loop_count", "most_relevant_open_loop",
    "connected_usb", "network_state", "conversation_state", "current_focus",
    "hardware_activity", "recent_hardware_events",
    "user_activity_state", "assistant_state", "kazumi_emotion", "dialogue_policy",
)
_INTEGRATIONS = ("proxmox", "openwrt", "home_assistant", "sentinel")
_PERSISTED_SLOTS = {
    "current_project", "recent_files", "recent_artifacts", "active_monitors", "active_tasks",
    "active_goal", "open_loop_count", "waiting_loop_count", "most_relevant_open_loop",
}
_TERMINAL_TASK_STATES = {"SUCCEEDED", "COMPLETED", "FAILED", "CANCELLED"}
_TERMINAL_MONITOR_STATES = {"COMPLETED", "FAILED", "CANCELLED"}
_GROUND_SOURCE_PREFIXES = (
    "artifact_context", "computer_perception", "conversation_engine", "desktop_controller",
    "event:", "homelab", "intelligence_tasks", "monitor_job", "network_watch",
    "open_loops_engine", "operator_task", "realtime_orchestrator", "sentinel", "tool:", "usb_monitor", "win32",
)
_DENIED_SOURCE_PREFIXES = ("assistant", "llm", "model", "prompt", "user")


class WorldStateEngine:
    """In-memory hot path plus selective atomic persistence.

    Values are accepted only from explicit grounded sources.  Free-form model
    output is never an observation and cannot promote a value to verified.
    """

    VERSION = 1
    MAX_TIMELINE = 100
    MAX_PERSISTED_TIMELINE = 25

    def __init__(self, event_bus: EventBus, *, persistence_path: Path,
                 clock: Clock = time.time) -> None:
        self.event_bus = event_bus
        self.persistence_path = Path(persistence_path)
        self.clock = clock
        self._slots: dict[str, _Record] = {}
        self._integrations: dict[str, _Record] = {}
        self._events: deque[WorldEvent] = deque(maxlen=self.MAX_TIMELINE)
        self._lock = RLock()
        self._started = False
        self._loaded = False
        self._last_error: str | None = None
        self._snapshot_latencies_ms: deque[float] = deque(maxlen=500)
        self._rejected_updates = 0

    async def start(self) -> None:
        if self._started:
            return
        self._load()
        await self.event_bus.subscribe(self.update_from_event)
        self._started = True
        self.update_verified(
            "assistant_state", "idle", source="realtime_orchestrator:init",
            confidence=1.0, ttl_seconds=None,
        )

    async def stop(self) -> None:
        if self._started:
            await self.event_bus.unsubscribe(self.update_from_event)
        self.persist()
        self._started = False

    # ------------------------------------------------------------ public API

    def get_snapshot(self) -> dict[str, Any]:
        started = time.perf_counter()
        now = self.clock()
        with self._lock:
            fields = {name: self._view(self._slots.get(name), now) for name in _SLOT_NAMES}
            fields["integration_state"] = {
                name: self._view(self._integrations.get(name), now) for name in _INTEGRATIONS
            }
            fields["recent_events"] = list(self._events)
        snapshot = WorldSnapshot(**fields).model_dump(mode="json")
        elapsed = (time.perf_counter() - started) * 1000
        with self._lock:
            self._snapshot_latencies_ms.append(elapsed)
        return snapshot

    def get_relevant_state(self, query: str = "", context: dict[str, Any] | None = None,
                           *, include_internal: bool = False) -> dict[str, Any]:
        del context  # reserved for structured turn metadata without changing the API
        wanted = self._relevant_slots(query)
        snapshot = self.get_snapshot()
        relevant: dict[str, Any] = {}
        for name in wanted:
            value = snapshot.get(name)
            if name == "integration_state":
                selected = self._relevant_integrations(query, value or {})
                if selected:
                    relevant[name] = selected
            elif value is not None and value.get("value") is not None:
                relevant[name] = self._without_internal_ids(name, value) if not include_internal else value
        return relevant

    def get_recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), self.MAX_TIMELINE))
        with self._lock:
            events = list(self._events)[-bounded:]
        return [item.model_dump(mode="json") for item in events]

    def get_current_focus(self) -> dict[str, Any] | None:
        now = self.clock()
        with self._lock:
            value = self._view(self._slots.get("current_focus"), now)
        if value is None or value.value is None:
            return None
        return value.model_dump(mode="json")

    def health(self) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            states = [
                self._freshness(item, now)
                for item in (*self._slots.values(), *self._integrations.values())
            ]
            latencies = list(self._snapshot_latencies_ms)
            return {
                "state": "READY" if self._started and self._last_error is None else (
                    "DEGRADED" if self._started else "OFFLINE"
                ),
                "started": self._started,
                "persistence_loaded": self._loaded,
                "tracked_slots": len(self._slots) + len(self._integrations),
                "fresh_slots": sum(value in {WorldFreshness.FRESH, WorldFreshness.PERSISTENT} for value in states),
                "stale_slots": sum(value == WorldFreshness.STALE for value in states),
                "recent_events": len(self._events),
                "rejected_updates": self._rejected_updates,
                "average_snapshot_latency_ms": round(sum(latencies) / len(latencies), 4) if latencies else 0.0,
                "last_error": self._last_error,
            }

    def update_verified(self, key: str, value: Any, *, source: str,
                        confidence: float = 1.0, ttl_seconds: float | None = None,
                        stale_after_seconds: float | None = None,
                        persist: bool | None = None, observed_at: float | None = None,
                        verified: bool = True) -> bool:
        """Grounded direct-ingestion boundary used by deterministic producers."""
        if key not in _SLOT_NAMES or value is None or not verified or not self._source_allowed(source):
            with self._lock:
                self._rejected_updates += 1
            return False
        return self._put(
            key, value, source=source, confidence=confidence,
            ttl_seconds=ttl_seconds, stale_after_seconds=stale_after_seconds,
            persist=(key in _PERSISTED_SLOTS if persist is None else persist),
            observed_at=observed_at,
        )

    async def update_from_event(self, event: Event) -> None:
        """Consume only event types whose producer is a known local authority."""
        event_type = str(getattr(event.type, "value", event.type))
        payload = event.payload if isinstance(event.payload, dict) else {}
        observed_at = event.timestamp.timestamp()

        if event.type == EventType.COMPUTER_WINDOW_FOREGROUND_CHANGED:
            self._foreground(payload, observed_at)
        elif event.type == EventType.PC_ACTIVE_WINDOW_CHANGED:
            app = str(payload.get("app") or "").strip()
            if app:
                self._put("current_app", self._app_identity(app), source="event:pc_awareness",
                          confidence=.9, ttl_seconds=6, stale_after_seconds=30, observed_at=observed_at)
        elif event.type == EventType.COMPUTER_WINDOW_CLOSED:
            self._window_closed(payload, observed_at)
        elif event.type == EventType.COMPUTER_APPLICATION_CLOSED:
            self._application_closed(payload, observed_at)
        elif event.type == EventType.COMPUTER_STATE_UPDATED:
            self._desktop_state(payload, observed_at)
        elif event.type == EventType.COMPUTER_BROWSER_NAVIGATION:
            self._browser_event(payload, observed_at)
        elif event_type == "artifact.context.updated":
            self._artifact_event(payload, observed_at)
        elif event.type in {EventType.COMPUTER_FILE_CREATED, EventType.COMPUTER_FILE_MODIFIED}:
            self._file_event(payload, observed_at, event_type)
        elif event.type in {EventType.TASK_STARTED, EventType.TASK_STATE_CHANGED, EventType.TASK_FINISHED}:
            self._task_event(payload, observed_at, str(payload.get("source") or "operator_task"))
        elif event.type in {EventType.AGENT_RUN_STARTED, EventType.AGENT_RUN_STATE_CHANGED,
                            EventType.AGENT_RUN_FINISHED, EventType.AGENT_RUN_CANCELLED}:
            self._agent_event(event.type, payload, observed_at)
        elif event.type in {EventType.MONITOR_JOB_CREATED, EventType.MONITOR_JOB_READING,
                            EventType.MONITOR_JOB_CHANGED, EventType.MONITOR_JOB_COMPLETED,
                            EventType.MONITOR_JOB_FAILED, EventType.MONITOR_JOB_CANCELLED}:
            self._monitor_event(payload, observed_at, event_type)
        elif event.type == EventType.OPEN_LOOP_SUMMARY_UPDATED:
            self._open_loop_summary_event(payload, observed_at)
        elif event.type in {EventType.USB_DEVICE_CONNECTED, EventType.USB_DEVICE_KNOWN_CONNECTED,
                            EventType.USB_DEVICE_UNKNOWN, EventType.USB_DEVICE_METADATA_CHANGED,
                            EventType.USB_DEVICE_COM_CHANGED, EventType.USB_DEVICE_DISCONNECTED,
                            EventType.USB_MONITOR_STARTED, EventType.USB_MONITOR_STOPPED}:
            self._usb_event(event.type, payload, observed_at)
        elif event.type == EventType.NETWORK_STATUS_UPDATED:
            self._put("network_state", self._safe_network(payload), source="network_watch",
                      confidence=1.0, ttl_seconds=90, stale_after_seconds=300, observed_at=observed_at)
        elif event.type in {
            EventType.NETWORK_GATEWAY_DOWN, EventType.NETWORK_GATEWAY_RECOVERED,
            EventType.NETWORK_INTERNET_DOWN, EventType.NETWORK_INTERNET_RECOVERED,
            EventType.NETWORK_DNS_FAILURE, EventType.NETWORK_DNS_RECOVERED,
            EventType.NETWORK_RECOVERED, EventType.NETWORK_INTERFACE_CHANGED,
            EventType.NETWORK_LINK_DOWN, EventType.NETWORK_LINK_UP,
            EventType.NETWORK_LATENCY_RECOVERED, EventType.NETWORK_JITTER_RECOVERED,
            EventType.NETWORK_PACKET_LOSS_RECOVERED,
        }:
            self._network_transition(event.type, payload, observed_at)
        elif event.type == EventType.SENTINEL_STATUS_CHANGED:
            self._put_integration("sentinel", self._safe_integration(payload), source="sentinel",
                                  confidence=1.0, ttl_seconds=300, observed_at=observed_at)
        elif event.type in {EventType.HOMELAB_HOST_ONLINE, EventType.HOMELAB_HOST_OFFLINE,
                            EventType.HOMELAB_HOST_DEGRADED}:
            self._homelab_event(payload, observed_at)
        elif event.type in {EventType.PROXMOX_VM_CHANGED, EventType.PROXMOX_TASK_COMPLETED,
                            EventType.PROXMOX_TASK_FAILED}:
            self._put_integration("proxmox", {"state": "READY", "event": event_type},
                                  source="homelab:proxmox", confidence=.98,
                                  ttl_seconds=300, observed_at=observed_at)
        elif event.type == EventType.HOME_ASSISTANT_ACTION_VERIFIED and payload.get("effect_verified") is True:
            self._put_integration("home_assistant", {"state": "READY", "event": event_type},
                                  source="homelab:home_assistant", confidence=1.0,
                                  ttl_seconds=300, observed_at=observed_at)
        elif event.type in {EventType.USER_IDLE, EventType.USER_RETURNED}:
            state = "ACTIVE" if event.type == EventType.USER_RETURNED else str(payload.get("level") or "IDLE").upper()
            self._put("user_activity_state", state, source="event:win32_idle", confidence=1.0,
                      ttl_seconds=10, stale_after_seconds=30, observed_at=observed_at)
        elif event.type == EventType.CONVERSATION_STATE_CHANGED:
            state = str(payload.get("state") or "").strip()
            if state:
                self._put("conversation_state", state, source="conversation_engine", confidence=1.0,
                          ttl_seconds=120, stale_after_seconds=600, observed_at=observed_at)
        elif event.type == EventType.KAZUMI_EMOTION_CHANGED:
            emotion = str(payload.get("emotion") or "").strip().casefold()
            policy = str(payload.get("dialogue_policy") or "").strip().casefold()
            if emotion:
                self._put(
                    "kazumi_emotion",
                    {"emotion": emotion, "intensity": float(payload.get("intensity") or 0.0)},
                    source="event:persona_runtime", confidence=1.0,
                    ttl_seconds=21600, stale_after_seconds=3600, observed_at=observed_at,
                )
            if policy:
                self._put(
                    "dialogue_policy", policy, source="event:persona_runtime",
                    confidence=1.0, ttl_seconds=21600,
                    stale_after_seconds=3600, observed_at=observed_at,
                )
        else:
            self._assistant_event(event.type, payload, observed_at)

    # ------------------------------------------------------- producer bridges

    def ingest_usb_snapshot(self, devices: list[dict], *, source: str) -> bool:
        """Only the successful native reconciliation renews USB freshness."""
        if source != "windows_setupapi" or any(item.get("metadata", {}).get("simulated") for item in devices):
            return False
        return self._put("connected_usb", [self._safe_usb(item) for item in devices],
                         source="usb_monitor:windows_setupapi", confidence=1.0,
                         ttl_seconds=45, stale_after_seconds=90, persist=False)

    def ingest_perception_snapshot(self, snapshot: dict[str, Any], *,
                                   user_activity_state: str | None = None) -> None:
        """Refresh short-TTL fields from the existing Win32 perception tick."""
        now = self.clock()
        foreground = snapshot.get("foreground_window")
        if isinstance(foreground, dict) and foreground:
            self._foreground(foreground, now)
        windows = snapshot.get("windows")
        if isinstance(windows, list):
            open_apps = []
            for window in windows:
                process = str((window or {}).get("process") or "").strip()
                if process:
                    identity = self._app_identity(process)
                    if identity not in open_apps:
                        open_apps.append(identity)
            previous = self._record_value("recent_apps") or []
            foreground_identity = self._record_value("current_app")
            recent = ([foreground_identity] if foreground_identity else []) + [
                app for app in previous if app in open_apps and app != foreground_identity
            ] + [app for app in open_apps if app != foreground_identity and app not in previous]
            self._put("recent_apps", recent[:12], source="computer_perception:win32",
                      confidence=1.0, ttl_seconds=8, stale_after_seconds=30, observed_at=now)
        recent_files = snapshot.get("recent_files")
        if isinstance(recent_files, list):
            safe_files = [self._safe_file(item) for item in recent_files[:25] if isinstance(item, dict)]
            safe_files = [item for item in safe_files if item.get("path")]
            self._put("recent_files", safe_files, source="computer_perception:filesystem",
                      confidence=.98, ttl_seconds=120, stale_after_seconds=600,
                      persist=True, observed_at=now)
        if user_activity_state in {"ACTIVE", "IDLE", "AWAY", "UNKNOWN"}:
            self._put("user_activity_state", user_activity_state,
                      source="win32:GetLastInputInfo", confidence=1.0,
                      ttl_seconds=6, stale_after_seconds=20, observed_at=now)

    async def observe_tool_result(self, tool_name: str, payload: dict[str, Any], result: Any,
                                  turn_id: str | None = None) -> None:
        del payload, turn_id
        data = result.data if hasattr(result, "data") else result if isinstance(result, dict) else {}
        ok = bool(getattr(result, "ok", data.get("success", False) if isinstance(data, dict) else False))
        if not ok or not isinstance(data, dict):
            return
        verified = data.get("effect_verified") is True or str(data.get("verification_status")) == "VERIFIED"
        if tool_name.startswith("browser_") and verified:
            self._browser_result(tool_name, data)
        if tool_name.startswith(("proxmox", "pve")) and verified:
            self._put_integration("proxmox", {"state": "READY", "tool": tool_name},
                                  source=f"tool:{tool_name}", confidence=1.0, ttl_seconds=300)
        elif tool_name.startswith(("home_assistant", "ha_")) and verified:
            self._put_integration("home_assistant", {"state": "READY", "tool": tool_name},
                                  source=f"tool:{tool_name}", confidence=1.0, ttl_seconds=300)
        elif tool_name.startswith("openwrt") and verified:
            self._put_integration("openwrt", {"state": "READY", "tool": tool_name},
                                  source=f"tool:{tool_name}", confidence=1.0, ttl_seconds=300)

    def synchronize_authorities(self, *, tasks: Iterable[dict[str, Any]] = (),
                                monitors: Iterable[dict[str, Any]] = (),
                                artifacts: Iterable[dict[str, Any]] = (),
                                integrations: dict[str, Any] | None = None,
                                network: dict[str, Any] | None = None) -> None:
        """One startup reconciliation from already loaded authoritative stores."""
        now = self.clock()
        active_tasks = [self._safe_task(item) for item in tasks if self._task_active(item)]
        active_monitors = [self._safe_monitor(item) for item in monitors if self._monitor_active(item)]
        self._put("active_tasks", active_tasks, source="operator_task:store", confidence=1.0,
                  ttl_seconds=None, persist=True, observed_at=now)
        self._put("active_monitors", active_monitors, source="monitor_job:store", confidence=1.0,
                  ttl_seconds=None, persist=True, observed_at=now)
        if active_tasks:
            self._put("current_task", active_tasks[0], source="operator_task:store", confidence=1.0,
                      ttl_seconds=None, observed_at=now)
        else:
            self._clear("current_task")
        safe_artifacts = [self._safe_artifact(item) for item in artifacts]
        safe_artifacts = [item for item in safe_artifacts if item.get("path")][-20:]
        if safe_artifacts:
            self._put("recent_artifacts", safe_artifacts, source="artifact_context:store",
                      confidence=1.0, ttl_seconds=None, persist=True, observed_at=now)
        if network:
            self._put("network_state", self._safe_network(network), source="network_watch:startup",
                      confidence=1.0, ttl_seconds=90, stale_after_seconds=300, observed_at=now)
        for name, value in (integrations or {}).items():
            if name in _INTEGRATIONS and isinstance(value, dict):
                self._put_integration(name, self._safe_integration(value), source=f"homelab:{name}:startup",
                                      confidence=.9, ttl_seconds=300, observed_at=now)
        self.persist()

    def context_summary(self, query: str) -> str:
        relevant = self.get_relevant_state(query)
        if not relevant:
            return ""
        lines = ["[WORLD STATE]"]
        for key, observed in relevant.items():
            if key == "integration_state":
                for name, integration in observed.items():
                    lines.append(self._context_line(name, integration))
            else:
                lines.append(self._context_line(key, observed))
        return "\n".join(lines)[:1800]

    def operations_view(self) -> dict[str, Any]:
        snapshot = self.get_snapshot()
        observations: list[dict[str, Any]] = []
        mapping = (
            ("Focus", "Current Focus", "current_focus"),
            ("Applications", "Current App", "current_app"),
            ("Applications", "Current Window", "current_window"),
            ("Tasks", "Active Tasks", "active_tasks"),
            ("Monitors", "Active Monitors", "active_monitors"),
            ("Open Loops", "Active Goal", "active_goal"),
            ("Open Loops", "Open", "open_loop_count"),
            ("Open Loops", "Waiting", "waiting_loop_count"),
            ("Open Loops", "Most Relevant", "most_relevant_open_loop"),
            ("Artifacts", "Recent Artifacts", "recent_artifacts"),
            ("USB", "Connected USB", "connected_usb"),
            ("Network", "Network", "network_state"),
        )
        for category, label, key in mapping:
            item = snapshot.get(key)
            observations.append(self._operations_observation(category, label, key, item))
        for name in _INTEGRATIONS:
            observations.append(self._operations_observation(
                "Integrations", name.replace("_", " ").title(), name,
                snapshot["integration_state"].get(name),
            ))
        grouped: dict[str, list[dict[str, Any]]] = {}
        for observation in observations:
            grouped.setdefault(observation["category"], []).append(observation)
        return {
            "generated_at": self.clock(),
            "categories": [
                {"category": name, "observations": values} for name, values in grouped.items()
            ],
            "total_observations": len(observations),
            "snapshot": snapshot,
            "health": self.health(),
        }

    # -------------------------------------------------------------- handlers

    def _foreground(self, payload: dict[str, Any], observed_at: float) -> None:
        process = str(payload.get("process") or payload.get("process_name") or "").strip()
        title = str(payload.get("title") or "").strip()
        hwnd = self._integer(payload.get("hwnd"))
        pid = self._integer(payload.get("pid"))
        if not process and not title:
            return
        identity = self._app_identity(process or title)
        window = {"hwnd": hwnd, "title": title, "process": process, "app": identity}
        self._put("current_window", window, source="computer_perception:win32", confidence=1.0,
                  ttl_seconds=6, stale_after_seconds=30, observed_at=observed_at)
        self._put("current_app", identity, source="computer_perception:win32", confidence=1.0,
                  ttl_seconds=6, stale_after_seconds=30, observed_at=observed_at)
        self._put("current_process", {"pid": pid, "name": process}, source="computer_perception:win32",
                  confidence=1.0, ttl_seconds=6, stale_after_seconds=30, observed_at=observed_at)
        self._put("current_desktop_target", window, source="computer_perception:win32", confidence=1.0,
                  ttl_seconds=6, stale_after_seconds=30, observed_at=observed_at)
        self._put("current_focus", {"kind": "window", **window}, source="computer_perception:win32",
                  confidence=1.0, ttl_seconds=6, stale_after_seconds=30, observed_at=observed_at)
        self._recent_app(identity, observed_at)
        self._timeline("desktop.foreground", f"{self._display_app(identity)} ficou foreground",
                       "computer_perception:win32", observed_at)

    def _window_closed(self, payload: dict[str, Any], observed_at: float) -> None:
        current = self._record_value("current_window") or {}
        hwnd = self._integer(payload.get("hwnd"))
        if hwnd and self._integer(current.get("hwnd")) == hwnd:
            for key in ("current_window", "current_app", "current_process", "current_desktop_target", "current_focus"):
                self._clear(key)
        label = str(payload.get("title") or payload.get("process") or "janela")[:120]
        self._timeline("desktop.window.closed", f"{label} fechou", "computer_perception:win32", observed_at)

    def _application_closed(self, payload: dict[str, Any], observed_at: float) -> None:
        process = str(payload.get("process") or "").casefold().removesuffix(".exe")
        current_process = str((self._record_value("current_process") or {}).get("name") or "").casefold().removesuffix(".exe")
        if process and process == current_process:
            for key in ("current_window", "current_app", "current_process", "current_desktop_target", "current_focus"):
                self._clear(key)
        recent = [item for item in (self._record_value("recent_apps") or [])
                  if str((item or {}).get("process") or "").casefold().removesuffix(".exe") != process]
        self._put("recent_apps", recent, source="computer_perception:win32", confidence=1.0,
                  ttl_seconds=8, stale_after_seconds=30, observed_at=observed_at)

    def _desktop_state(self, payload: dict[str, Any], observed_at: float) -> None:
        if payload.get("verified") is not True:
            return
        action = str(payload.get("action") or "").upper()
        target = str(payload.get("target") or "").strip()
        if action in {"FOCUS_APP", "SWITCH_APP", "OPEN_APP", "MINIMIZE_APP", "MAXIMIZE_APP", "RESTORE_APP"} and target:
            value = {"kind": "app", "app": self._app_identity(target), "action": action}
            self._put("current_desktop_target", value, source="desktop_controller:verified_action",
                      confidence=1.0, ttl_seconds=10, stale_after_seconds=60, observed_at=observed_at)
        self._put("current_operation", {"action": action, "target": target, "state": "COMPLETED"},
                  source="desktop_controller:verified_action", confidence=1.0,
                  ttl_seconds=120, stale_after_seconds=600, observed_at=observed_at)

    def _artifact_event(self, payload: dict[str, Any], observed_at: float) -> None:
        if payload.get("verified") is not True or not isinstance(payload.get("artifact"), dict):
            return
        artifact = self._safe_artifact(payload["artifact"])
        if not artifact.get("path"):
            return
        recent = [item for item in (self._record_value("recent_artifacts") or [])
                  if item.get("path") != artifact["path"]]
        recent = [*recent, artifact][-20:]
        self._put("recent_artifacts", recent, source="artifact_context", confidence=1.0,
                  ttl_seconds=None, persist=True, observed_at=observed_at)
        self._put("current_file", artifact, source="artifact_context", confidence=1.0,
                  ttl_seconds=120, stale_after_seconds=600, observed_at=observed_at)
        self._timeline("artifact.updated", f"Artefato atualizado: {artifact.get('display_name') or artifact['path']}",
                       "artifact_context", observed_at)

    def _file_event(self, payload: dict[str, Any], observed_at: float, event_type: str) -> None:
        path = str(payload.get("path") or "").strip()
        if not path:
            return
        item = {"path": path, "name": Path(path).name, "root": payload.get("root")}
        files = [existing for existing in (self._record_value("recent_files") or [])
                 if existing.get("path") != path]
        self._put("recent_files", [item, *files][:25], source="computer_perception:filesystem",
                  confidence=.98, ttl_seconds=120, stale_after_seconds=600,
                  persist=True, observed_at=observed_at)
        self._put("current_file", item, source="computer_perception:filesystem", confidence=.8,
                  ttl_seconds=30, stale_after_seconds=120, observed_at=observed_at)
        self._timeline(event_type, f"Arquivo observado: {item['name']}",
                       "computer_perception:filesystem", observed_at)

    def _task_event(self, payload: dict[str, Any], observed_at: float, source: str) -> None:
        item = self._safe_task(payload)
        task_id = item.get("task_id")
        if not task_id:
            return
        existing = next((task for task in self._record_value("active_tasks") or []
                         if task.get("task_id") == task_id), {})
        item = {**existing, **{key: value for key, value in item.items() if value not in (None, "")}}
        tasks = [task for task in (self._record_value("active_tasks") or [])
                 if task.get("task_id") != task_id]
        if str(item.get("state") or "").upper() not in _TERMINAL_TASK_STATES:
            tasks.insert(0, item)
            self._put("current_task", item, source=source, confidence=1.0,
                      ttl_seconds=None, observed_at=observed_at)
        elif (self._record_value("current_task") or {}).get("task_id") == task_id:
            if tasks:
                self._put("current_task", tasks[0], source=source, confidence=1.0,
                          ttl_seconds=None, observed_at=observed_at)
            else:
                self._clear("current_task")
        self._put("active_tasks", tasks[:50], source=source, confidence=1.0,
                  ttl_seconds=None, persist=True, observed_at=observed_at)
        self._timeline("task.transition", f"Task {item.get('state') or 'UPDATED'}: {item.get('objective') or task_id}",
                       source, observed_at)

    def _agent_event(self, event_type: EventType, payload: dict[str, Any], observed_at: float) -> None:
        mapped = dict(payload)
        mapped["task_id"] = payload.get("agent_run_id") or payload.get("run_id")
        mapped["objective"] = payload.get("goal") or "Agent Run"
        if event_type == EventType.AGENT_RUN_STARTED:
            mapped["state"] = "RUNNING"
        elif event_type in {EventType.AGENT_RUN_FINISHED, EventType.AGENT_RUN_CANCELLED}:
            mapped["state"] = "CANCELLED" if event_type == EventType.AGENT_RUN_CANCELLED else "COMPLETED"
        self._task_event(mapped, observed_at, "operator_task:agent_run")

    def _monitor_event(self, payload: dict[str, Any], observed_at: float, event_type: str) -> None:
        item = self._safe_monitor(payload)
        monitor_id = item.get("monitor_id")
        if not monitor_id:
            return
        existing = next((monitor for monitor in self._record_value("active_monitors") or []
                         if monitor.get("monitor_id") == monitor_id), {})
        item = {**existing, **{key: value for key, value in item.items() if value not in (None, "")}}
        monitors = [monitor for monitor in (self._record_value("active_monitors") or [])
                    if monitor.get("monitor_id") != monitor_id]
        status = str(item.get("status") or "ACTIVE").upper()
        if status not in _TERMINAL_MONITOR_STATES and not event_type.endswith(("completed", "failed", "cancelled")):
            monitors.insert(0, item)
        self._put("active_monitors", monitors[:50], source="monitor_job", confidence=1.0,
                  ttl_seconds=None, persist=True, observed_at=observed_at)
        self._timeline("monitor.transition", f"Monitor {status}: {item.get('objective') or monitor_id}",
                       "monitor_job", observed_at)

    def _open_loop_summary_event(self, payload: dict[str, Any], observed_at: float) -> None:
        source = "open_loops_engine"
        for key in ("active_goal", "most_relevant_open_loop"):
            value = payload.get(key)
            if value is None:
                self._clear(key)
            else:
                self._put(key, value, source=source, confidence=1.0,
                          ttl_seconds=None, persist=True, observed_at=observed_at)
        for key in ("open_loop_count", "waiting_loop_count"):
            self._put(key, max(0, int(payload.get(key) or 0)), source=source,
                      confidence=1.0, ttl_seconds=None, persist=True,
                      observed_at=observed_at)

    def _usb_event(self, event_type: EventType, payload: dict[str, Any], observed_at: float) -> None:
        if payload.get("simulated") is True:
            return
        devices = list(self._record_value("connected_usb") or [])
        if event_type == EventType.USB_MONITOR_STARTED:
            synced = payload.get("connected_devices")
            if isinstance(synced, list):
                devices = [self._safe_usb(item) for item in synced if isinstance(item, dict)
                           and not item.get("metadata", {}).get("simulated")]
        elif event_type == EventType.USB_MONITOR_STOPPED:
            devices = []
        else:
            device = payload.get("device")
            if not isinstance(device, dict) or not device.get("device_id"):
                return
            if device.get("metadata", {}).get("simulated"):
                return
            safe = self._safe_usb(device)
            devices = [item for item in devices if item.get("device_id") != safe["device_id"]]
            if event_type != EventType.USB_DEVICE_DISCONNECTED:
                devices.append(safe)
            label = safe.get("friendly_name") or safe.get("name") or safe["device_id"]
            verb = "desconectado" if event_type == EventType.USB_DEVICE_DISCONNECTED else "conectado"
            self._timeline(str(event_type.value), f"USB {label} {verb}", "usb_monitor", observed_at)
        self._put("connected_usb", devices, source="usb_monitor", confidence=1.0,
                  ttl_seconds=45, stale_after_seconds=90, persist=False, observed_at=observed_at)

    def _network_transition(self, event_type: EventType, payload: dict[str, Any], observed_at: float) -> None:
        current = dict(self._record_value("network_state") or {})
        recoveries = {
            EventType.NETWORK_RECOVERED, EventType.NETWORK_GATEWAY_RECOVERED,
            EventType.NETWORK_INTERNET_RECOVERED, EventType.NETWORK_DNS_RECOVERED,
            EventType.NETWORK_LINK_UP, EventType.NETWORK_LATENCY_RECOVERED,
            EventType.NETWORK_JITTER_RECOVERED, EventType.NETWORK_PACKET_LOSS_RECOVERED,
        }
        current["status"] = "online" if event_type in recoveries else "degraded"
        current["last_transition"] = str(event_type.value)
        self._put("network_state", current, source="network_watch", confidence=1.0,
                  ttl_seconds=90, stale_after_seconds=300, observed_at=observed_at)
        self._timeline(str(event_type.value), str(payload.get("message") or event_type.value)[:180],
                       "network_watch", observed_at)

    def _homelab_event(self, payload: dict[str, Any], observed_at: float) -> None:
        host = str(payload.get("host") or "").casefold()
        integration = next((name for name in ("proxmox", "openwrt", "home_assistant")
                            if name in host.replace("-", "_")), None)
        if integration:
            self._put_integration(integration, {"state": payload.get("state") or "UNKNOWN", "host": host},
                                  source=f"homelab:{integration}", confidence=1.0,
                                  ttl_seconds=300, observed_at=observed_at)

    def _assistant_event(self, event_type: EventType, payload: dict[str, Any],
                         observed_at: float) -> None:
        state: str | None = None
        if event_type in {EventType.USER_SPEECH_STARTED, EventType.STT_STARTED,
                          EventType.WAKE_WORD_DETECTED, EventType.HANDS_FREE_STARTED}:
            state = "listening"
        elif event_type in {EventType.LLM_PROCESSING, EventType.LLM_STREAM_STARTED,
                            EventType.STT_COMPLETED}:
            state = "thinking"
        elif event_type in {EventType.SHELL_EXECUTION_STARTED, EventType.REMOTE_SHELL_EXECUTION_STARTED,
                            EventType.AGENT_RUN_STEP, EventType.COMPUTER_ACTION_EXECUTED}:
            state = "acting"
        elif event_type in {EventType.TTS_STARTED, EventType.PLAYBACK_STARTED}:
            state = "speaking"
        elif event_type in {EventType.TTS_FINISHED, EventType.TTS_FAILED, EventType.SPEECH_CANCELLED,
                            EventType.USER_INTERRUPTED, EventType.KAZUMI_RESPONSE,
                            EventType.HANDS_FREE_ENDED, EventType.SHELL_EXECUTION_FINISHED,
                            EventType.REMOTE_SHELL_EXECUTION_FINISHED,
                            EventType.COMPUTER_EFFECT_VERIFIED}:
            state = "idle"
        if state:
            self._put("assistant_state", state, source="realtime_orchestrator:event",
                      confidence=1.0, ttl_seconds=None, observed_at=observed_at)
        if event_type in {EventType.SHELL_EXECUTION_STARTED, EventType.REMOTE_SHELL_EXECUTION_STARTED}:
            self._put(
                "current_operation",
                {
                    "type": "remote_shell" if event_type == EventType.REMOTE_SHELL_EXECUTION_STARTED else "system_shell",
                    "state": "RUNNING",
                    "operation_id": str(payload.get("execution_id") or payload.get("tool_call_id") or "")[:100],
                },
                source="event:shell", confidence=1.0,
                ttl_seconds=120, stale_after_seconds=600, observed_at=observed_at,
            )
        elif event_type in {EventType.SHELL_EXECUTION_FINISHED, EventType.REMOTE_SHELL_EXECUTION_FINISHED}:
            current = dict(self._record_value("current_operation") or {})
            if current.get("type") in {"system_shell", "remote_shell"}:
                current["state"] = "COMPLETED"
                self._put("current_operation", current, source="event:shell", confidence=1.0,
                          ttl_seconds=30, stale_after_seconds=120, observed_at=observed_at)

    def _browser_result(self, tool_name: str, data: dict[str, Any]) -> None:
        detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
        browser = detail.get("browser") or data.get("app") or "browser"
        tab = detail.get("tab") if isinstance(detail.get("tab"), dict) else None
        if tab is None and isinstance(detail.get("tabs"), list) and detail["tabs"]:
            tab = detail["tabs"][0]
        self._put("current_browser", self._app_identity(str(browser)), source=f"tool:{tool_name}",
                  confidence=1.0, ttl_seconds=60, stale_after_seconds=300)
        if tab:
            safe_tab = {"id": str(tab.get("id") or "")[:80], "title": str(tab.get("title") or "")[:200],
                        "url": self._safe_url(tab.get("url"))}
            self._put("current_tab", safe_tab, source=f"tool:{tool_name}", confidence=1.0,
                      ttl_seconds=60, stale_after_seconds=300)
            if safe_tab["url"]:
                self._put("current_url", safe_tab["url"], source=f"tool:{tool_name}", confidence=1.0,
                          ttl_seconds=60, stale_after_seconds=300)
        self._timeline("browser.navigation", f"Navegador atualizado por {tool_name}",
                       f"tool:{tool_name}", self.clock())

    def _browser_event(self, payload: dict[str, Any], observed_at: float) -> None:
        url = self._safe_url(payload.get("url"))
        browser = str(payload.get("browser") or payload.get("app") or "browser")
        tab = payload.get("tab") if isinstance(payload.get("tab"), dict) else {
            "id": str(payload.get("tab_id") or "")[:80],
            "title": str(payload.get("title") or "")[:200],
            "url": url,
        }
        self._put("current_browser", self._app_identity(browser),
                  source="event:browser", confidence=1.0,
                  ttl_seconds=60, stale_after_seconds=300, observed_at=observed_at)
        self._put("current_tab", tab, source="event:browser", confidence=1.0,
                  ttl_seconds=60, stale_after_seconds=300, observed_at=observed_at)
        if url:
            self._put("current_url", url, source="event:browser", confidence=1.0,
                      ttl_seconds=60, stale_after_seconds=300, observed_at=observed_at)

    # --------------------------------------------------------------- storage

    def persist(self) -> bool:
        try:
            with self._lock:
                slots = {
                    key: self._record_document(record)
                    for key, record in self._slots.items()
                    if record.persist and key in _PERSISTED_SLOTS
                }
                events = [item.model_dump(mode="json") for item in list(self._events)
                          if item.source.startswith(("artifact_context", "monitor_job", "operator_task"))]
                payload = {
                    "version": self.VERSION,
                    "saved_at": self.clock(),
                    "slots": slots,
                    "recent_events": events[-self.MAX_PERSISTED_TIMELINE:],
                }
            self.persistence_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.persistence_path.with_suffix(self.persistence_path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
                                 encoding="utf-8")
            os.replace(temporary, self.persistence_path)
            self._last_error = None
            return True
        except (OSError, TypeError, ValueError) as error:
            self._last_error = type(error).__name__
            return False

    def _load(self) -> None:
        if not self.persistence_path.is_file():
            self._loaded = True
            return
        try:
            document = json.loads(self.persistence_path.read_text(encoding="utf-8"))
            if document.get("version") != self.VERSION:
                raise ValueError("unsupported world-state version")
            for key, raw in (document.get("slots") or {}).items():
                if key not in _PERSISTED_SLOTS or not isinstance(raw, dict):
                    continue
                source = str(raw.get("source") or "")
                if not raw.get("verified") or not self._source_allowed(source):
                    continue
                self._slots[key] = _Record(
                    value=raw.get("value"), source=source,
                    observed_at=float(raw.get("observed_at") or 0),
                    confidence=max(0.0, min(1.0, float(raw.get("confidence") or 0))),
                    verified=True, ttl_seconds=None, stale_after_seconds=None, persist=True,
                )
            for raw in (document.get("recent_events") or [])[-self.MAX_PERSISTED_TIMELINE:]:
                try:
                    self._events.append(WorldEvent.model_validate(raw))
                except ValueError:
                    continue
            self._last_error = None
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._last_error = type(error).__name__
        self._loaded = True

    # --------------------------------------------------------------- helpers

    def _put(self, key: str, value: Any, *, source: str, confidence: float,
             ttl_seconds: float | None, stale_after_seconds: float | None = None,
             persist: bool = False, observed_at: float | None = None) -> bool:
        if value is None or not self._source_allowed(source):
            with self._lock:
                self._rejected_updates += 1
            return False
        observed = self.clock() if observed_at is None else float(observed_at)
        ttl = None if ttl_seconds is None else max(0.01, float(ttl_seconds))
        stale_after = None if ttl is None else max(ttl, float(stale_after_seconds or ttl * 5))
        record = _Record(
            value=value, source=source, observed_at=observed,
            confidence=max(0.0, min(1.0, float(confidence))), verified=True,
            ttl_seconds=ttl, stale_after_seconds=stale_after, persist=bool(persist),
        )
        with self._lock:
            previous = self._slots.get(key)
            persistence_changed = bool(
                record.persist
                and (
                    previous is None
                    or previous.value != record.value
                    or previous.source != record.source
                    or not previous.persist
                )
            )
            self._slots[key] = record
        if persistence_changed:
            self.persist()
        return True

    def _put_integration(self, name: str, value: Any, *, source: str, confidence: float,
                         ttl_seconds: float | None, observed_at: float | None = None) -> None:
        if name not in _INTEGRATIONS or value is None or not self._source_allowed(source):
            return
        observed = self.clock() if observed_at is None else float(observed_at)
        with self._lock:
            self._integrations[name] = _Record(
                value=value, source=source, observed_at=observed,
                confidence=confidence, verified=True, ttl_seconds=ttl_seconds,
                stale_after_seconds=(ttl_seconds * 3 if ttl_seconds is not None else None), persist=False,
            )

    def _clear(self, key: str) -> None:
        with self._lock:
            self._slots.pop(key, None)

    def _record_value(self, key: str) -> Any:
        with self._lock:
            record = self._slots.get(key)
            return record.value if record is not None else None

    def _view(self, record: _Record | None, now: float) -> WorldValue | None:
        if record is None:
            return None
        freshness = self._freshness(record, now)
        # STALE/EXPIRED is diagnostic provenance, never current state.
        value = record.value if freshness in {WorldFreshness.FRESH, WorldFreshness.PERSISTENT} else None
        return WorldValue(
            value=value, source=record.source,
            observed_at=datetime.fromtimestamp(record.observed_at, timezone.utc),
            confidence=record.confidence, freshness=freshness, verified=record.verified,
        )

    @staticmethod
    def _freshness(record: _Record, now: float) -> WorldFreshness:
        if record.ttl_seconds is None:
            return WorldFreshness.PERSISTENT
        age = max(0.0, now - record.observed_at)
        if age <= record.ttl_seconds:
            return WorldFreshness.FRESH
        if record.stale_after_seconds is not None and age <= record.stale_after_seconds:
            return WorldFreshness.STALE
        return WorldFreshness.EXPIRED

    @staticmethod
    def _source_allowed(source: str) -> bool:
        normalized = str(source or "").strip().casefold()
        return bool(normalized) and not normalized.startswith(_DENIED_SOURCE_PREFIXES) and normalized.startswith(_GROUND_SOURCE_PREFIXES)

    @staticmethod
    def _app_identity(value: str) -> dict[str, str]:
        from app.desktop.canonical_apps import display_name_for, family_for

        clean = str(value or "").strip()
        family = family_for(clean)
        return {
            "canonical_id": family.canonical_id if family else clean.casefold().removesuffix(".exe").replace(" ", "_"),
            "display_name": family.display_name if family else display_name_for(clean),
            "process": clean if clean.casefold().endswith(".exe") else "",
        }

    def _recent_app(self, identity: dict[str, str], observed_at: float) -> None:
        canonical = identity.get("canonical_id")
        recent = [item for item in (self._record_value("recent_apps") or [])
                  if item.get("canonical_id") != canonical]
        self._put("recent_apps", [identity, *recent][:12], source="computer_perception:win32",
                  confidence=1.0, ttl_seconds=8, stale_after_seconds=30, observed_at=observed_at)

    def _timeline(self, event_type: str, summary: str, source: str, observed_at: float) -> None:
        safe = " ".join(str(summary or "").replace("\x00", "").split())[:240]
        if not safe:
            return
        event = WorldEvent(event_type=event_type, summary=safe, source=source,
                           observed_at=datetime.fromtimestamp(observed_at, timezone.utc), verified=True)
        with self._lock:
            last = self._events[-1] if self._events else None
            if last and last.event_type == event.event_type and last.summary == event.summary:
                self._events[-1] = event
            else:
                self._events.append(event)

    @staticmethod
    def _safe_task(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": str(item.get("task_id") or item.get("id") or "")[:100],
            "type": str(item.get("type") or item.get("action") or "operator")[:80],
            "objective": str(item.get("objective") or item.get("goal") or item.get("title") or "")[:500],
            "state": str(getattr(item.get("state"), "value", item.get("state") or ""))[:60],
        }

    @staticmethod
    def _safe_monitor(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "monitor_id": str(item.get("monitor_id") or "")[:100],
            "type": str(item.get("probe_tool") or item.get("type") or "monitor")[:80],
            "objective": str(item.get("objective") or "")[:500],
            "status": str(getattr(item.get("status"), "value", item.get("status") or "ACTIVE"))[:60],
        }

    @staticmethod
    def _safe_artifact(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "artifact_id": str(item.get("artifact_id") or "")[:100],
            "display_name": str(item.get("display_name") or Path(str(item.get("path") or "")).name)[:240],
            "path": str(item.get("path") or "")[:1200],
            "kind": str(item.get("kind") or "file")[:60],
            "host_scope": str(item.get("host_scope") or "local")[:40],
            "exists_state": str(item.get("exists_state") or "unknown")[:40],
        }

    @staticmethod
    def _safe_file(item: dict[str, Any]) -> dict[str, Any]:
        path = str(item.get("path") or "")[:1200]
        return {"path": path, "name": Path(path).name, "mtime": item.get("mtime"),
                "size": item.get("size"), "root": item.get("root")}

    @staticmethod
    def _safe_usb(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "device_id": str(item.get("device_id") or "")[:100],
            "name": str(item.get("name") or "")[:200],
            "friendly_name": str(item.get("friendly_name") or "")[:200] or None,
            "category": str(item.get("category") or "")[:80] or None,
            "com_port": str(item.get("com_port") or "")[:40] or None,
            "drive_letter": str(item.get("drive_letter") or "")[:20] or None,
            "known": bool(item.get("known") or item.get("registered")),
            "status": str(item.get("status") or "CONNECTED")[:40],
        }

    @staticmethod
    def _safe_network(item: dict[str, Any]) -> dict[str, Any]:
        snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
        interface = snapshot.get("interface") if isinstance(snapshot.get("interface"), dict) else {}
        quality = snapshot.get("quality") if isinstance(snapshot.get("quality"), dict) else {}
        gateway = snapshot.get("gateway_state") if isinstance(snapshot.get("gateway_state"), dict) else {}
        dns = snapshot.get("dns_state") if isinstance(snapshot.get("dns_state"), dict) else {}
        internet = snapshot.get("internet_state") if isinstance(snapshot.get("internet_state"), dict) else {}
        return {
            "enabled": bool(item.get("enabled", True)),
            "running": bool(item.get("running", True)),
            "status": str(item.get("status") or "unknown")[:40],
            "internet_reachable": snapshot.get("internet_reachable", item.get("internet_reachable")),
            "dns_ok": snapshot.get("dns_ok", item.get("dns_ok")),
            "latency_ms": snapshot.get("internet_latency_ms", item.get("latency_ms")),
            "packet_loss_percent": snapshot.get("packet_loss_percent", item.get("packet_loss_percent")),
            "health": str(snapshot.get("health") or item.get("health") or "unknown")[:40],
            "active_interface": interface.get("name", snapshot.get("interface_name")),
            "gateway_state": gateway.get("state"),
            "dns_state": dns.get("state"),
            "internet_state": internet.get("state"),
            "jitter_ms": quality.get("jitter_ms", snapshot.get("jitter_ms")),
            "rx_bytes_per_sec": interface.get("rx_bytes_per_sec", snapshot.get("rx_bytes_per_sec")),
            "tx_bytes_per_sec": interface.get("tx_bytes_per_sec", snapshot.get("tx_bytes_per_sec")),
        }

    @staticmethod
    def _safe_integration(item: dict[str, Any]) -> dict[str, Any]:
        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        return {
            "enabled": item.get("enabled", status.get("enabled")),
            "configured": item.get("configured"),
            "connected": item.get("connected"),
            "state": str(item.get("state") or status.get("state") or "UNKNOWN")[:60],
            "health": str(item.get("health") or "")[:60] or None,
        }

    @staticmethod
    def _safe_url(value: Any) -> str:
        url = str(value or "")[:2000]
        # Userinfo can contain credentials; never retain it in state/context.
        return re.sub(r"(?i)^(https?://)[^/@\s]+@", r"\1", url)

    @staticmethod
    def _integer(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _task_active(item: dict[str, Any]) -> bool:
        state = str(getattr(item.get("state"), "value", item.get("state") or "")).upper()
        return bool(state and state not in _TERMINAL_TASK_STATES)

    @staticmethod
    def _monitor_active(item: dict[str, Any]) -> bool:
        status = str(getattr(item.get("status"), "value", item.get("status") or "")).upper()
        return status == "ACTIVE"

    @staticmethod
    def _record_document(record: _Record) -> dict[str, Any]:
        return {
            "value": record.value, "source": record.source,
            "observed_at": record.observed_at, "confidence": record.confidence,
            "verified": record.verified,
        }

    @staticmethod
    def _display_app(identity: Any) -> str:
        if isinstance(identity, dict):
            return str(identity.get("display_name") or identity.get("canonical_id") or "aplicativo")
        return str(identity or "aplicativo")

    @staticmethod
    def _without_internal_ids(name: str, observed: dict[str, Any]) -> dict[str, Any]:
        copied = dict(observed)
        if name in {"active_tasks", "active_monitors"} and isinstance(copied.get("value"), list):
            copied["value"] = [
                {key: value for key, value in item.items() if key not in {"task_id", "monitor_id"}}
                for item in copied["value"]
            ]
        elif name == "most_relevant_open_loop" and isinstance(copied.get("value"), dict):
            copied["value"] = {
                key: value for key, value in copied["value"].items() if key != "id"
            }
        return copied

    @staticmethod
    def _relevant_slots(query: str) -> set[str]:
        text = " ".join(str(query or "").casefold().split())
        selected: set[str] = set()
        groups = (
            (("app", "aplicativo", "aberto", "aberta", "janela", "foreground", "foco", "minimiza", "fecha", "ele", "ela"),
             {"current_app", "current_window", "current_focus", "current_desktop_target", "recent_apps"}),
            (("browser", "navegador", "url", "site", "aba", "chrome", "edge"),
             {"current_browser", "current_url", "current_tab"}),
            (("task", "tarefa", "operação", "fazendo", "executando"),
             {"current_task", "active_tasks", "current_operation"}),
            (("monitor", "monitoramento", "acompanhando"), {"active_monitors"}),
            (("pendente", "pendencia", "aberto", "open loop", "retoma", "retomar", "continuar", "objetivo", "goal", "aguardando"),
             {"active_goal", "open_loop_count", "waiting_loop_count", "most_relevant_open_loop"}),
            (("arquivo", "file", "artefato", "artifact", "log", "relatório", "report", "projeto"),
             {"current_file", "recent_files", "recent_artifacts", "current_project"}),
            (("usb", "pendrive", "dispositivo"), {"connected_usb"}),
            (("rede", "network", "internet", "dns", "latência", "conexão"), {"network_state"}),
            (("proxmox", "openwrt", "home assistant", "sentinel", "integração", "homelab"),
             {"integration_state"}),
            (("atividade", "ausente", "idle", "away"), {"user_activity_state"}),
            (("conversa", "ouvindo", "falando", "pensando", "estado"),
             {"conversation_state", "assistant_state"}),
        )
        for needles, slots in groups:
            if any(needle in text for needle in needles):
                selected.update(slots)
        if selected & {"current_desktop_target", "current_focus"}:
            selected.update({"current_task", "current_operation"})
        return selected

    @staticmethod
    def _relevant_integrations(query: str, values: dict[str, Any]) -> dict[str, Any]:
        text = str(query or "").casefold()
        named = [name for name in _INTEGRATIONS if name.replace("_", " ") in text]
        if "home assistant" in text:
            named.append("home_assistant")
        names = set(named) or set(_INTEGRATIONS)
        return {name: value for name, value in values.items()
                if name in names and value is not None and value.get("value") is not None}

    def _context_line(self, key: str, observed: dict[str, Any]) -> str:
        value = observed.get("value")
        if key in {"active_tasks", "active_monitors"} and isinstance(value, list):
            labels = [str(item.get("objective") or item.get("type") or "")[:80] for item in value[:3]]
            rendered = f"{len(value)}" + (f" ({'; '.join(labels)})" if labels else "")
        elif key in {"recent_apps"} and isinstance(value, list):
            rendered = ", ".join(self._display_app(item) for item in value[:6]) or "none"
        elif key == "recent_artifacts" and isinstance(value, list):
            rendered = ", ".join(str(item.get("display_name") or item.get("path") or "") for item in value[-4:])
        elif key in {"current_app", "current_browser"}:
            rendered = self._display_app(value)
        elif isinstance(value, dict):
            rendered = str(value.get("title") or value.get("objective") or value.get("status")
                           or value.get("state") or value.get("display_name") or value)[:300]
        elif isinstance(value, list):
            rendered = str(len(value))
        else:
            rendered = str(value)
        return f"{key}: {rendered} [{observed.get('freshness')}; {observed.get('source')}]"

    def _operations_observation(self, category: str, label: str, key: str,
                                item: dict[str, Any] | None) -> dict[str, Any]:
        value = item.get("value") if item else None
        state = "UNKNOWN"
        detail = "Não observado"
        if item and value is not None:
            state = "READY" if item.get("freshness") in {"FRESH", "PERSISTENT"} else "STALE"
            if isinstance(value, list):
                detail = f"{len(value)} item(ns)"
            elif key in {"current_app", "current_browser"}:
                detail = self._display_app(value)
            elif isinstance(value, dict):
                detail = str(value.get("title") or value.get("status") or value.get("state") or value)[:160]
            else:
                detail = str(value)[:160]
        return {
            "category": category, "name": label, "state": state,
            "source": item.get("source") if item else "world_state",
            "observed_at": (datetime.fromisoformat(item["observed_at"]).timestamp()
                            if item and item.get("observed_at") else 0),
            "freshness": item.get("freshness") if item else "UNKNOWN",
            "verification": "verified" if item and item.get("verified") else "unverified",
            "detail": detail,
        }
