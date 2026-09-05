"""OperatorV2Service: façade that wires every V2 capability (spec §7).

The Agent Controller remains central; this service only exposes capabilities.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.operator.contexts import OperatorContextRegistry
from app.operator.credentials import CredentialBroker
from app.operator.elevated_sessions import ElevatedSessionManager
from app.operator.jobs import PersistentJobManager
from app.operator.monitoring import MonitorJobManager, ProactiveMonitorNotifications
from app.operator.proactive_rules import ProactiveOperator
from app.operator.recovery import RecoveryEngine
from app.operator.tasks import OperatorTaskManager
from app.operator.vision import VisionEngine
from app.operator.watcher import DesktopWatcher
from app.operator.workflows import WorkflowEngine

logger = logging.getLogger("nyra.operator.v2")


class OperatorV2Service:
    """Aggregate lifecycle + status for all operator-v2 capabilities."""

    def __init__(self, settings, event_bus, approvals, registry,
                 browser_controller=None, proactive_gate=None,
                 speech_queue=None, tts_provider_getter=None,
                 voice_processor=None, credential_broker=None,
                 legacy_monitor_notifications: bool = True) -> None:
        from app.operator.browser_v2 import BrowserV2Controller
        from app.operator.adapters import create_adapter_registry
        from app.operator.clipboard import ClipboardController

        self.settings = settings
        self.event_bus = event_bus
        self.approvals = approvals
        self.registry = registry
        self.clipboard = ClipboardController()

        vision_on = bool(getattr(settings, "vision_enabled", True))
        self.vision_enabled = vision_on
        self.contexts = OperatorContextRegistry()
        self.vision = VisionEngine(
            approvals,
            frame_ttl_seconds=float(getattr(settings, "vision_frame_ttl_seconds", 45)),
            debug_keep_frames=bool(getattr(settings, "vision_debug_keep_frames", False)),
        ) if vision_on else None

        self.browser_controller = browser_controller
        if browser_controller is not None and bool(getattr(settings, "browser_control_enabled", True)):
            self.browser_v2 = BrowserV2Controller(browser_controller.manager)
            self.adapters = create_adapter_registry(browser_controller.manager)
            self.browser_control_enabled = True
        else:
            self.browser_v2 = None
            self.adapters = create_adapter_registry(None)
            self.browser_control_enabled = False

        self.credentials_enabled = bool(getattr(settings, "credential_broker_enabled", True))
        self.credentials = (
            credential_broker or CredentialBroker(approvals)
        ) if self.credentials_enabled else None

        self.elevated = ElevatedSessionManager(
            approvals,
            default_ttl_seconds=int(getattr(settings, "elevated_session_default_ttl_seconds", 300)),
        )

        self.jobs_enabled = bool(getattr(settings, "persistent_jobs_enabled", True))
        self.jobs = PersistentJobManager(event_bus) if self.jobs_enabled else None
        self.monitor_jobs = MonitorJobManager(
            registry, event_bus, database_path=settings.database_path,
        )
        self.monitor_notifications = (
            ProactiveMonitorNotifications(
                settings, event_bus, speech_queue, tts_provider_getter,
                voice_processor=voice_processor,
            )
            if legacy_monitor_notifications and speech_queue is not None and tts_provider_getter is not None
            else None
        )

        self.workflow_engine_enabled = bool(getattr(settings, "workflow_engine_enabled", True))
        self.workflows = WorkflowEngine(registry, event_bus) if self.workflow_engine_enabled else None
        if self.workflows is not None and approvals is not None:
            # Permite ao engine verificar estado de aprovação pendente no resume (§53/§57).
            self.workflows.approval_lookup = approvals.get

        self.tasks = OperatorTaskManager(registry, approvals, jobs=self.jobs,
                                         recovery=None, event_bus=event_bus)

        self.recovery = RecoveryEngine(approvals, event_bus)
        self.tasks.recovery = self.recovery

        watcher_enabled = bool(getattr(settings, "desktop_watcher_enabled", True))
        self.watcher = DesktopWatcher(
            event_bus,
            default_ttl_seconds=int(getattr(settings, "watch_default_ttl_seconds", 300)),
        ) if watcher_enabled else None

        self.proactive = ProactiveOperator(
            proactive_gate, self.workflows, event_bus,
            enabled=bool(getattr(settings, "proactive_operator_enabled", False)),
        )

        self._started = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._started:
            return
        if self.jobs is not None:
            await self.jobs.initialize()
            self.jobs.start_monitor()
        await self.monitor_jobs.initialize()
        self.monitor_jobs.start()
        if self.monitor_notifications is not None:
            await self.monitor_notifications.start()
        await self.recovery.initialize()
        if self.watcher is not None:
            await self.watcher.start()
        await self.tasks.initialize()
        if self.workflows is not None:
            seeded = self.workflows.seed_templates()
            if not seeded.get("success"):
                logger.warning("workflow template seed failed: %s", seeded)
        self.event_task = asyncio.create_task(self._subscribe_events(), name="nyra-operator-v2-events")
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        if hasattr(self, "event_task") and not self.event_task.done():
            self.event_task.cancel()
            try:
                await self.event_task
            except asyncio.CancelledError:
                pass
        if hasattr(self, "_event_forwarder"):
            await self.event_bus.unsubscribe(self._event_forwarder)
        await self.tasks.shutdown()
        await self.monitor_jobs.shutdown()
        if self.monitor_notifications is not None:
            await self.monitor_notifications.stop()
        if self.jobs is not None:
            await self.jobs.shutdown()
        if self.watcher is not None:
            await self.watcher.stop()
        if self.vision is not None:
            await asyncio.to_thread(self.vision.shutdown)
        if self.browser_controller is not None:
            await asyncio.to_thread(self.browser_controller.manager.shutdown)
        self._started = False

    async def _subscribe_events(self) -> None:
        from app.events import Event

        from app.operator.watchdog_bridge import WatchdogBridge

        self.watchdog_bridge = WatchdogBridge()

        async def forwarder(event: Event) -> None:
            try:
                await self.proactive.handle_event(event)
                # §226-§228: supervisor vivo pode pedir restart externo.
                await self.watchdog_bridge.handle_event(event)
            except Exception:  # noqa: BLE001
                pass

        self._event_forwarder = forwarder
        await self.event_bus.subscribe(forwarder)

    # --------------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        return {
            "operator_v2": True,
            "flags": {
                "vision": self.vision_enabled,
                "browser_control": self.browser_control_enabled,
                "credentials": self.credentials_enabled,
                "persistent_jobs": self.jobs_enabled,
                "workflow_engine": self.workflow_engine_enabled,
                "desktop_watcher": self.watcher is not None,
                "proactive_operator": getattr(self.settings, "proactive_operator_enabled", False),
                "clipboard": True,
            },
            "contexts": self.contexts.snapshot(),
            "watches": self.watcher.status() if self.watcher else {"running": False},
            "workflows_count": len(self.workflows.list_workflows()["workflows"]) if self.workflows else 0,
            "tasks_active": sum(
                1 for item in []
            ) or None,
            "monitor_jobs_active": sum(
                1 for item in self.monitor_jobs._jobs.values() if item.status.value == "ACTIVE"
            ),
            "elevated_sessions": self.elevated.status()["active_sessions"],
            "proactive": self.proactive.status(),
        }


async def create_operator_v2_service(settings, event_bus, approvals, registry,
                                     browser_controller=None,
                                     proactive_gate=None,
                                     speech_queue=None,
                                     tts_provider_getter=None,
                                     voice_processor=None,
                                     credential_broker=None,
                                     legacy_monitor_notifications: bool = True) -> OperatorV2Service:
    service = OperatorV2Service(settings, event_bus, approvals, registry,
                                browser_controller=browser_controller,
                                proactive_gate=proactive_gate,
                                speech_queue=speech_queue,
                                tts_provider_getter=tts_provider_getter,
                                voice_processor=voice_processor,
                                credential_broker=credential_broker,
                                legacy_monitor_notifications=legacy_monitor_notifications)
    return service
