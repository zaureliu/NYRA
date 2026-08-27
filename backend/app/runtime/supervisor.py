"""Runtime Supervisor: lifecycle, health/readiness, locks, crash-loop protection.

Owns persistent services registered in config/runtime_services.yaml. Mutations are
locked per service, verified after execution (ACT -> VERIFY) and reported with
grounding fields. External services are never terminated by NYRA shutdown.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.core.paths import LOG_ROOT, PROJECT_ROOT
from app.core.turn import current_turn_id
from app.events import EventBus, EventType
from app.runtime.health import run_health_check
from app.runtime.history import RuntimeHistory
from app.runtime.logs import read_log_tail
from app.runtime.models import (
    HealthResult,
    OperationErrorCodes,
    Ownership,
    ReadinessKind,
    RuntimeState,
    RuntimeType,
    ServiceSnapshot,
    ServiceSpec,
    operation_result,
)
from app.runtime.process_manager import ProcessManager, rotate_log_file
from app.runtime.registry import RuntimeRegistry, load_runtime_registry

logger = logging.getLogger("nyra.runtime")


class RuntimeSupervisor:
    def __init__(
        self,
        settings: Any,
        event_bus: EventBus,
        registry: RuntimeRegistry | None = None,
        *,
        process_manager: ProcessManager | None = None,
        history: RuntimeHistory | None = None,
        hooks: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.registry = registry or RuntimeRegistry()
        self.processes = process_manager or ProcessManager()
        self.history = history or RuntimeHistory(settings.database_path)
        self.hooks = hooks or {}
        self.snapshots: dict[str, ServiceSnapshot] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._prev_states: dict[str, str] = {}
        self._restart_marks: dict[str, deque[float]] = {}
        self._recovery_state: dict[str, dict[str, float]] = {}
        self._alert_cooldowns: dict[str, float] = {}
        self._monitor_task: asyncio.Task | None = None
        self._stopping = False
        self.lock_wait_seconds: float = 10.0

    # ------------------------------------------------------------------ init

    async def initialize(self) -> None:
        await self.history.initialize()
        self.registry = load_runtime_registry(
            self.settings.runtime_services_path,
            python_exe=sys.executable,
            repo_root=str(PROJECT_ROOT),
        )
        for entry in self.registry.entries:
            if entry.spec is not None:
                snapshot = self._base_snapshot(entry.spec)
                snapshot.state = RuntimeState.DISABLED if not entry.spec.enabled else RuntimeState.UNKNOWN
            else:
                snapshot = ServiceSnapshot(
                    id=entry.service_id, state=RuntimeState.INVALID_CONFIGURATION,
                    validation_error=(entry.error or "invalid"),
                )
            self.snapshots[entry.service_id] = snapshot
        await self.inspect_all()
        if getattr(self.settings, "runtime_supervisor_enabled", True):
            self.start_monitor()

    def _base_snapshot(self, spec: ServiceSpec) -> ServiceSnapshot:
        capabilities = spec.capabilities.model_dump()
        return ServiceSnapshot(
            id=spec.id, display_name=spec.display_name, ownership=spec.ownership.value,
            type=spec.type.value if isinstance(spec.type, str) else str(spec.type),
            capabilities=capabilities, startup_policy=spec.startup_policy.value,
        )

    async def inspect_all(self) -> None:
        specs = [entry.spec for entry in self.registry.entries if entry.spec is not None]
        for group_start in range(0, len(specs), 4):
            batch = specs[group_start:group_start + 4]
            results = await asyncio.gather(*(self.inspect(spec.id) for spec in batch))
            _ = results

    # ------------------------------------------------------------- utilities

    def _spec(self, service_id: str) -> ServiceSpec | None:
        return self.registry.get(service_id)

    def _snapshot(self, service_id: str) -> ServiceSnapshot | None:
        return self.snapshots.get(service_id)

    async def _with_lock(self, service_id: str, action: Callable[[], Awaitable[dict]]) -> dict:
        lock = self._locks.setdefault(service_id, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=self.lock_wait_seconds)
        except TimeoutError:
            return operation_result(
                success=False, service=service_id, action="locked", state=self._state_of(service_id),
                error_code=OperationErrorCodes.OPERATION_LOCKED,
                message="Outra operação de runtime está em andamento para este serviço.",
            )
        try:
            return await action()
        finally:
            if lock.locked():
                lock.release()

    def _state_of(self, service_id: str) -> RuntimeState:
        snapshot = self._snapshot(service_id)
        return snapshot.state if snapshot else RuntimeState.UNKNOWN

    async def _set_state(self, service_id: str, new_state: RuntimeState, *, error: str | None = None) -> None:
        snapshot = self._snapshot(service_id)
        if snapshot is None or snapshot.state == new_state:
            if snapshot is not None and error:
                snapshot.last_error = error
            return
        previous = snapshot.state
        self._prev_states[service_id] = previous.value
        snapshot.state = new_state
        if error is not None:
            snapshot.last_error = error
        elif new_state in {RuntimeState.READY, RuntimeState.RUNNING}:
            snapshot.last_error = None
        turn_id = current_turn_id.get()
        await self.event_bus.publish(
            EventType.RUNTIME_STATUS_CHANGED,
            service=service_id,
            state=new_state.value,
            turn_id=turn_id,
        )
        event_map = {
            RuntimeState.STARTING: EventType.RUNTIME_STARTING,
            RuntimeState.RUNNING: EventType.RUNTIME_RUNNING,
            RuntimeState.READY: EventType.RUNTIME_READY,
            RuntimeState.STOPPING: EventType.RUNTIME_STOPPING,
            RuntimeState.STOPPED: EventType.RUNTIME_STOPPED,
            RuntimeState.RESTARTING: EventType.RUNTIME_RESTARTING,
            RuntimeState.FAILED: EventType.RUNTIME_FAILED,
            RuntimeState.CRASH_LOOP: EventType.RUNTIME_CRASH_LOOP,
        }
        specific = event_map.get(new_state)
        if specific:
            await self.event_bus.publish(specific, service=service_id, state=new_state.value, turn_id=turn_id)
        if previous in {RuntimeState.DEGRADED, RuntimeState.FAILED} and new_state == RuntimeState.READY:
            await self.event_bus.publish(EventType.RUNTIME_RECOVERED, service=service_id, turn_id=turn_id)

    def _register_failure_mark(self, service_id: str) -> bool:
        """Record a failed start/crash timestamp. Returns True when crash-loop threshold is reached."""
        marks = self._restart_marks.setdefault(service_id, deque(maxlen=64))
        now = time.monotonic()
        window = float(getattr(self.settings, "runtime_restart_window_seconds", 600))
        while marks and now - marks[0] > window:
            marks.popleft()
        marks.append(now)
        max_restarts = int(getattr(self.settings, "runtime_max_restarts", 3))
        return len(marks) >= max_restarts

    async def _finalize_start_failure(self, service_id: str, *, process_died: bool, error: str) -> None:
        tripped = self._register_failure_mark(service_id) if process_died else False
        if tripped:
            await self._set_state(service_id, RuntimeState.CRASH_LOOP, error="restart limit reached")
            await self.event_bus.publish(
                EventType.RUNTIME_CRASH_LOOP,
                service=service_id,
                turn_id=current_turn_id.get(),
            )
        elif process_died:
            await self._set_state(service_id, RuntimeState.FAILED, error=error)
        else:
            await self._set_state(service_id, RuntimeState.DEGRADED, error=error)

    def _crash_loop_expired(self, service_id: str) -> bool:
        marks = self._restart_marks.get(service_id)
        if not marks:
            return True
        window = float(getattr(self.settings, "runtime_restart_window_seconds", 600))
        return (time.monotonic() - marks[0]) > window

    async def _evaluate_readiness(self, spec: ServiceSpec, health: HealthResult | None) -> tuple[bool, str]:
        readiness = spec.readiness
        if readiness.kind == ReadinessKind.NONE:
            return True, "NONE"
        if readiness.kind == ReadinessKind.HEALTH_PASS:
            return bool(health and health.healthy), "HEALTH_PASS"
        if readiness.kind == ReadinessKind.OLLAMA_WARM:
            hook = self.hooks.get("warm_manager")
            if hook is None:
                return False, "OLLAMA_WARM_UNAVAILABLE"
            try:
                status = hook.status()
            except Exception as exc:  # noqa: BLE001
                return False, f"OLLAMA_WARM_ERROR:{type(exc).__name__}"
            state = str(status.get("state") or "") if isinstance(status, dict) else ""
            return state == "OLLAMA_READY", state or "UNKNOWN"
        return False, "UNSUPPORTED"

    # ---------------------------------------------------------------- inspect

    async def inspect(self, service_id: str) -> ServiceSnapshot:
        snapshot = self._snapshot(service_id)
        if snapshot is None:
            raise KeyError(f"Serviço não registrado: {service_id}")
        spec = self._spec(service_id)
        if spec is None:
            snapshot.state = RuntimeState.INVALID_CONFIGURATION
            return snapshot
        if not spec.enabled:
            snapshot.state = RuntimeState.DISABLED
            return snapshot

        managed = self.processes.get(service_id) if spec.type == RuntimeType.PROCESS else None
        health_result: HealthResult | None = None
        if spec.health is not None and spec.health.kind.value != "NONE":
            health_result = await run_health_check(spec, self.hooks)
            snapshot.health = {
                "healthy": health_result.healthy,
                "latency_ms": health_result.latency_ms,
                "detail": health_result.detail,
                "checked_at": health_result.checked_at.isoformat(),
            }

        if spec.type == RuntimeType.PROCESS:
            if managed is not None:
                snapshot.pid = managed.identity.pid
                snapshot.uptime_seconds = round(managed.uptime_seconds() or 0, 1)
            else:
                # Sem PID rastreado: qualquer health configurado que passe prova execução
                # real (ex.: backend iniciado pelo launcher antes do supervisor).
                detected_alive = bool(health_result and health_result.healthy)
                snapshot.pid = None
                snapshot.uptime_seconds = None
                if not detected_alive:
                    if snapshot.state != RuntimeState.CRASH_LOOP:
                        snapshot.state = RuntimeState.STOPPED
                    return snapshot
        ready, readiness_detail = await self._evaluate_readiness(spec, health_result)
        snapshot.readiness = readiness_detail

        healthy = bool(health_result and health_result.healthy) if (spec.health and spec.health.kind.value != "NONE") else managed is not None
        if healthy and ready:
            new_state = RuntimeState.READY
        elif healthy:
            new_state = RuntimeState.RUNNING
        elif health_result is not None and not health_result.healthy:
            previous = snapshot.state
            new_state = (
                RuntimeState.DEGRADED if managed is not None
                else RuntimeState.FAILED if previous in {RuntimeState.READY, RuntimeState.RUNNING}
                else RuntimeState.STOPPED
            )
        else:
            new_state = RuntimeState.STOPPED
        await self._set_state(service_id, new_state, error=None if healthy else ((health_result.detail if health_result else "") or None))
        return snapshot

    async def inspect_all_public(self) -> list[ServiceSnapshot]:
        await self.inspect_all()
        return [self.snapshots[key] for key in sorted(self.snapshots)]

    # ------------------------------------------------------------ operations

    async def start(
        self,
        service_id: str,
        *,
        origin: str = "operator",
        agent_run_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict:
        return await self._with_lock(service_id, lambda: self._start_locked(
            service_id, origin=origin, agent_run_id=agent_run_id, approval_id=approval_id,
        ))

    async def _start_locked(
        self,
        service_id: str,
        *,
        origin: str = "operator",
        agent_run_id: str | None = None,
        approval_id: str | None = None,
        started: float | None = None,
    ) -> dict:
        started = started if started is not None else time.perf_counter()

        def fail(code: OperationErrorCodes, message: str, state: RuntimeState | None = None) -> dict:
            return operation_result(
                success=False, service=service_id, action="start",
                state=state or self._state_of(service_id), error_code=code.value,
                message=message, duration_ms=(time.perf_counter() - started) * 1000,
                execution_success=False, effect_verified=False, verification_status="EXECUTION_FAILED",
            )

        spec = self._spec(service_id)
        if spec is None:
            return fail(OperationErrorCodes.UNKNOWN_SERVICE, "Serviço não registrado.")
        if not spec.enabled:
            return fail(OperationErrorCodes.SERVICE_DISABLED, "Serviço desabilitado no registry.", RuntimeState.DISABLED)
        if not spec.capabilities.start:
            return fail(OperationErrorCodes.CAPABILITY_DENIED, "Capability start não habilitada para este serviço.")

        current = await self.inspect(service_id)
        if current.state in {RuntimeState.READY, RuntimeState.RUNNING, RuntimeState.STARTING}:
            return operation_result(
                success=True, service=service_id, action="start", state=current.state,
                message=f"already_running ({current.state.value}); nenhuma segunda instância foi criada.",
                duration_ms=(time.perf_counter() - started) * 1000,
                execution_success=True, effect_verified=True, verification_status="VERIFIED",
                detail={"already_running": True, "pid": current.pid},
            )
        if current.state == RuntimeState.CRASH_LOOP:
            if not self._crash_loop_expired(service_id):
                return fail(OperationErrorCodes.CRASH_LOOP_PROTECTED, "Proteção de crash-loop ativa; aguarde a janela expirar.")
            self._restart_marks.pop(service_id, None)
        for dep in spec.depends_on:
            dep_snapshot = self._snapshot(dep)
            if dep_snapshot is None or dep_snapshot.state not in {RuntimeState.READY, RuntimeState.RUNNING}:
                return fail(OperationErrorCodes.DEPENDENCY_NOT_READY, f"Dependência '{dep}' não está pronta; inicie-a antes.")

        if spec.type != RuntimeType.PROCESS:
            return fail(OperationErrorCodes.UNSUPPORTED_RUNTIME, f"Tipo {spec.type.value} sem adapter nesta etapa.")
        if not spec.start_command:
            return fail(OperationErrorCodes.INVALID_CONFIGURATION, "start_command ausente.")

        log_path = spec.resolved_log_path(LOG_ROOT)
        rotate_log_file(log_path, spec.log_max_bytes, spec.log_backup_count)
        await self._set_state(service_id, RuntimeState.STARTING)
        try:
            working_dir = Path(spec.working_directory or str(PROJECT_ROOT))
            managed = await self.processes.spawn(service_id, spec.start_command, working_dir, log_path)
        except (OSError, ValueError) as exc:
            await self._set_state(service_id, RuntimeState.FAILED, error=f"spawn falhou: {type(exc).__name__}")
            result = fail(OperationErrorCodes.SPAWN_FAILED, f"spawn falhou: {type(exc).__name__}: {str(exc)[:120]}")
            await self._persist(service_id, "start", origin, agent_run_id, approval_id, result)
            return result

        deadline = time.monotonic() + float(spec.startup_timeout_seconds or getattr(self.settings, "runtime_default_startup_timeout_seconds", 30))
        final_state = RuntimeState.STARTING
        last_error = ""
        while time.monotonic() < deadline:
            await asyncio.sleep(0.75)
            if not managed.alive():
                final_state = RuntimeState.FAILED
                last_error = "processo encerrou imediatamente após spawn; consulte runtime_logs"
                break
            snapshot = await self.inspect(service_id)
            final_state = snapshot.state
            if final_state in {RuntimeState.READY, RuntimeState.RUNNING}:
                break
        if final_state not in {RuntimeState.READY, RuntimeState.RUNNING}:
            process_alive = managed.alive()
            await self._finalize_start_failure(
                service_id,
                process_died=not process_alive,
                error=last_error or ("processo encerrou durante startup" if not process_alive else "startup timeout sem readiness"),
            )
            result = operation_result(
                success=False, service=service_id, action="start",
                state=self._state_of(service_id),
                error_code=(
                    OperationErrorCodes.SPAWN_FAILED.value
                    if not process_alive
                    else OperationErrorCodes.STARTUP_TIMEOUT.value
                ),
                message=last_error or "serviço não ficou saudável dentro do startup timeout.",
                duration_ms=(time.perf_counter() - started) * 1000,
                execution_success=True, effect_verified=False, verification_status="VERIFICATION_FAILED",
                detail={"log_hint": "use runtime_logs para diagnóstico", "process_alive": process_alive},
            )
            await self._persist(service_id, "start", origin, agent_run_id, approval_id, result)
            return result

        snapshot = self._snapshot(service_id)
        result = operation_result(
            success=True, service=service_id, action="start", state=snapshot.state if snapshot else RuntimeState.READY,
            message="iniciado e verificado por health/readiness reais.",
            duration_ms=(time.perf_counter() - started) * 1000,
            execution_success=True, effect_verified=True, verification_status="VERIFIED",
            detail={"pid": snapshot.pid if snapshot else None},
        )
        await self.event_bus.publish(
            EventType.RUNTIME_HEALTH_PASSED,
            service=service_id,
            turn_id=current_turn_id.get(),
        )
        await self._persist(service_id, "start", origin, agent_run_id, approval_id, result)
        return result

    async def stop(
        self,
        service_id: str,
        *,
        origin: str = "operator",
        agent_run_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict:
        return await self._with_lock(service_id, lambda: self._stop_locked(
            service_id, origin=origin, agent_run_id=agent_run_id, approval_id=approval_id,
        ))

    async def _stop_locked(
        self,
        service_id: str,
        *,
        origin: str = "operator",
        agent_run_id: str | None = None,
        approval_id: str | None = None,
        started: float | None = None,
    ) -> dict:
        started = started if started is not None else time.perf_counter()

        def fail(code: OperationErrorCodes, message: str, state: RuntimeState | None = None) -> dict:
            return operation_result(
                success=False, service=service_id, action="stop",
                state=state or self._state_of(service_id), error_code=code.value,
                message=message, duration_ms=(time.perf_counter() - started) * 1000,
                execution_success=False, effect_verified=False, verification_status="EXECUTION_FAILED",
            )

        spec = self._spec(service_id)
        if spec is None:
            return fail(OperationErrorCodes.UNKNOWN_SERVICE, "Serviço não registrado.")
        if not spec.capabilities.stop:
            return fail(OperationErrorCodes.CAPABILITY_DENIED, "Capability stop não habilitada para este serviço.")
        if spec.type != RuntimeType.PROCESS:
            return fail(OperationErrorCodes.UNSUPPORTED_RUNTIME, f"Tipo {spec.type.value} sem adapter de stop nesta etapa.")

        current = await self.inspect(service_id)
        if current.state in {RuntimeState.STOPPED, RuntimeState.DISABLED, RuntimeState.INVALID_CONFIGURATION}:
            return operation_result(
                success=True, service=service_id, action="stop", state=current.state,
                message="already stopped.",
                duration_ms=(time.perf_counter() - started) * 1000,
                execution_success=True, effect_verified=True, verification_status="VERIFIED",
                detail={"already_stopped": True},
            )
        managed = self.processes.get(service_id)
        if managed is None:
            return fail(OperationErrorCodes.STOP_FAILED, "Nenhum processo rastreado com identidade válida para parar.")
        await self._set_state(service_id, RuntimeState.STOPPING)
        stopped = await self.processes.graceful_stop(managed, spec.stop_grace_seconds)
        if not stopped:
            await self._set_state(service_id, RuntimeState.FAILED, error="processo não confirmou parada após fallback")
            result = fail(OperationErrorCodes.STOP_FAILED, "Processo não confirmou parada mesmo após fallback controlado.")
            await self._persist(service_id, "stop", origin, agent_run_id, approval_id, result)
            return result
        await self._set_state(service_id, RuntimeState.STOPPED)
        result = operation_result(
            success=True, service=service_id, action="stop", state=RuntimeState.STOPPED,
            message="parado e ausência verificada.",
            duration_ms=(time.perf_counter() - started) * 1000,
            execution_success=True, effect_verified=True, verification_status="VERIFIED",
            detail={"pid": managed.identity.pid},
        )
        await self._persist(service_id, "stop", origin, agent_run_id, approval_id, result)
        return result

    async def restart(
        self,
        service_id: str,
        *,
        origin: str = "operator",
        agent_run_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict:
        started = time.perf_counter()

        def base_fail(code: OperationErrorCodes, message: str) -> dict:
            return operation_result(
                success=False, service=service_id, action="restart",
                state=self._state_of(service_id), error_code=code.value, message=message,
                duration_ms=(time.perf_counter() - started) * 1000,
                execution_success=False, effect_verified=False, verification_status="EXECUTION_FAILED",
            )

        spec = self._spec(service_id)
        if spec is None:
            return base_fail(OperationErrorCodes.UNKNOWN_SERVICE, "Serviço não registrado.")
        if spec.id == "nyra_backend" and spec.ownership == Ownership.OWNED:
            return base_fail(OperationErrorCodes.SELF_RESTART_UNSUPPORTED,
                             "Self-restart do backend exige supervisor externo; não implementado nesta etapa (limitação declarada).")
        if not spec.capabilities.restart:
            return base_fail(OperationErrorCodes.CAPABILITY_DENIED, "Capability restart não habilitada para este serviço.")

        async def run() -> dict:
            await self._set_state(service_id, RuntimeState.RESTARTING)
            stop_result = await self._stop_locked(service_id, origin=origin, agent_run_id=agent_run_id, approval_id=approval_id, started=started)
            if not (stop_result.get("success") and stop_result.get("error_code") in {None, OperationErrorCodes.ALREADY_STOPPED.value}):
                failure = operation_result(
                    success=False, service=service_id, action="restart",
                    state=self._state_of(service_id),
                    error_code=OperationErrorCodes.STOP_FAILED.value,
                    message=f"restart abortado no STOP: {stop_result.get('message')}",
                    duration_ms=(time.perf_counter() - started) * 1000,
                    execution_success=False, effect_verified=False, verification_status="VERIFICATION_FAILED",
                )
                await self._set_state(service_id, RuntimeState.FAILED, error="restart falhou na fase STOP")
                await self._persist(service_id, "restart", origin, agent_run_id, approval_id, failure)
                return failure
            start_result = await self._start_locked(service_id, origin=origin, agent_run_id=agent_run_id, approval_id=approval_id, started=started)
            final_state = self._state_of(service_id)
            restart_ok = start_result.get("success") and final_state in {RuntimeState.READY, RuntimeState.RUNNING}
            result = operation_result(
                success=bool(restart_ok), service=service_id, action="restart", state=final_state,
                error_code=start_result.get("error_code"),
                message="restart concluído com health/readiness verificados." if restart_ok else f"restart não confirmado: {start_result.get('message')}",
                duration_ms=(time.perf_counter() - started) * 1000,
                execution_success=True, effect_verified=restart_ok,
                verification_status="VERIFIED" if restart_ok else "VERIFICATION_FAILED",
                detail={"stop": {"state": stop_result.get("state")}, "pid": start_result.get("pid")},
            )
            await self._persist(service_id, "restart", origin, agent_run_id, approval_id, result)
            return result

        return await self._with_lock(service_id, run)

    # ------------------------------------------------------------------ misc

    async def health(self, service_id: str) -> dict:
        spec = self._spec(service_id)
        snapshot = self._snapshot(service_id)
        if spec is None or snapshot is None:
            return {"success": False, "service": service_id, "error_code": OperationErrorCodes.UNKNOWN_SERVICE.value}
        if spec.health is None or spec.health.kind.value == "NONE":
            return {"success": True, "service": service_id, "health": None, "message": "sem health check configurado"}
        result = await run_health_check(spec, self.hooks)
        if result:
            snapshot.health = {
                "healthy": result.healthy, "latency_ms": result.latency_ms,
                "detail": result.detail, "checked_at": result.checked_at.isoformat(),
            }
        await self._emit_health_event(service_id, healthy=bool(result and result.healthy), detail=result.detail if result else "")
        payload = {
            "success": True, "service": service_id,
            "health": snapshot.health,
            "verification_status": "VERIFIED",
        }
        return payload

    async def logs(self, service_id: str, lines: int | None = None) -> dict:
        spec = self._spec(service_id)
        if spec is None:
            return {"success": False, "service": service_id, "error_code": OperationErrorCodes.UNKNOWN_SERVICE.value}
        tail = read_log_tail(
            spec.resolved_log_path(LOG_ROOT),
            lines=int(lines or getattr(self.settings, "runtime_log_tail_lines", 100)),
            max_chars=int(getattr(self.settings, "runtime_log_max_chars", 50_000)),
        )
        return {"success": True, "service": service_id, **tail}

    async def recover_if_needed(self, service_id: str) -> dict | None:
        spec = self._spec(service_id)
        snapshot = self._snapshot(service_id)
        if spec is None or snapshot is None or not spec.auto_recovery.enabled:
            return None
        configured_allowlist = [
            item.strip() for item in
            str(getattr(self.settings, "runtime_auto_recovery_services", "")).split(",") if item.strip()
        ]
        # Allowlist vazia = conservador: nenhuma auto-recovery permitida (#32).
        if service_id not in configured_allowlist:
            return None
        if snapshot.state not in {RuntimeState.FAILED, RuntimeState.DEGRADED}:
            return None
        bookkeeping = self._recovery_state.setdefault(service_id, {})
        cooldown = float(spec.auto_recovery.cooldown_seconds)
        last_attempt = float(bookkeeping.get("last_attempt", 0))
        now = time.monotonic()
        if now - last_attempt < cooldown:
            return None
        attempts_window = float(getattr(self.settings, "runtime_restart_window_seconds", 600))
        attempts = [ts for ts in bookkeeping.setdefault("attempts", []) if now - ts < attempts_window]
        if len(attempts) >= spec.auto_recovery.max_attempts:
            await self._set_state(service_id, RuntimeState.CRASH_LOOP, error="auto-recovery exauriu tentativas")
            return None
        backoff_schedule = [2.0, 5.0, 10.0]
        delay = backoff_schedule[min(len(attempts), len(backoff_schedule) - 1)]
        await asyncio.sleep(min(delay, 10))
        bookkeeping["last_attempt"] = time.monotonic()
        bookkeeping["attempts"] = attempts + [time.monotonic()]
        result = await self.restart(service_id, origin="auto_recovery")
        if result.get("success"):
            bookkeeping["attempts"] = []
        return result

    async def _monitor_loop(self) -> None:
        interval = float(getattr(self.settings, "runtime_health_interval_seconds", 15))
        while not self._stopping:
            await asyncio.sleep(interval)
            try:
                for entry in self.registry.entries:
                    spec = entry.spec
                    if spec is None or not spec.enabled or spec.health is None:
                        continue
                    before = self._snapshot(spec.id)
                    was_unhealthy = bool(before and before.state in {RuntimeState.DEGRADED, RuntimeState.FAILED})
                    await self.inspect(spec.id)
                    after = self._snapshot(spec.id)
                    unhealthy_now = bool(after and after.state in {RuntimeState.DEGRADED, RuntimeState.FAILED})
                    if unhealthy_now and not was_unhealthy:
                        await self._emit_health_event(spec.id, healthy=False, detail=str(after.last_error or ""), force_alert=True)
                    if unhealthy_now:
                        await self.recover_if_needed(spec.id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - monitor must survive individual failures
                logger.warning("runtime_monitor_iteration_failed", extra={"error_type": type(exc).__name__})
    def start_monitor(self) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_loop(), name="nyra-runtime-monitor")

    async def shutdown(self) -> None:
        self._stopping = True
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        for entry in self.registry.entries:
            spec = entry.spec
            if spec is None or spec.type != RuntimeType.PROCESS or spec.ownership != Ownership.OWNED:
                continue
            if spec.shutdown_policy.value != "TERMINATE":
                continue
            managed = self.processes.get(spec.id)
            if managed:
                await self.processes.graceful_stop(managed, spec.stop_grace_seconds)

    async def _emit_health_event(self, service_id: str, *, healthy: bool, detail: str, force_alert: bool = False) -> None:
        if healthy:
            await self.event_bus.publish(
                EventType.RUNTIME_HEALTH_PASSED,
                service=service_id,
                detail=detail,
                turn_id=current_turn_id.get(),
            )
            return
        now = time.monotonic()
        cooldown = float(getattr(self.settings, "runtime_alert_cooldown_seconds", 120))
        last = self._alert_cooldowns.get(service_id, 0)
        if force_alert or now - last >= cooldown:
            self._alert_cooldowns[service_id] = now
            await self.event_bus.publish(
                EventType.RUNTIME_HEALTH_FAILED,
                service=service_id,
                detail=redact(detail),
                turn_id=current_turn_id.get(),
            )

    async def _persist(
        self, service_id: str, action: str, origin: str, agent_run_id: str | None,
        approval_id: str | None, result: dict,
    ) -> None:
        try:
            await self.history.add(
                service=service_id, action=action, origin=origin,
                previous_state=self._prev_states.get(service_id, ""), new_state=str(result.get("state", "")),
                duration_ms=float(result.get("duration_ms", 0)), success=bool(result.get("success")),
                error_code=result.get("error_code"), agent_run_id=agent_run_id, approval_id=approval_id,
            )
        except Exception as exc:  # noqa: BLE001 - audit must never break operations
            logger.warning("runtime_history_write_failed", extra={"error_type": type(exc).__name__})
        logger.info(
            "runtime_operation",
            extra={
                "service": service_id, "action": action, "origin": origin,
                "success": bool(result.get("success")), "state": str(result.get("state")),
                "duration_ms": result.get("duration_ms"), "agent_run_id": agent_run_id,
                "approval_required": bool(result.get("approval_required")),
            },
        )


def redact(value: str) -> str:
    from app.tools.redaction import redact_secrets

    return redact_secrets(str(value)[:300])
