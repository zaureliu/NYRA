"""Homelab Control Plane controller.

Central capability layer used by both the Agent tools and the HTTP API:
- unified host resolution through the Host Registry;
- concurrent bounded health probes;
- native Proxmox API operations with task grounding (UPID → wait → verify);
- Home Assistant REST reads/actions with effect verification;
- SSH-backed OpenWrt/Linux/Windows adapters over the Trusted SSH layer;
- short-TTL read-only cache (never applied to actions or verifications);
- per-resource mutation locks, single-use approvals via the shared gate;
- operational history and cooldown-guarded normalized events.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from app.agent.context import current_agent_run_id
from app.core.config import Settings
from app.core.turn import current_turn_id
from app.events import EventBus, EventType
from app.homelab.adapters.base import SshAdapterError
from app.homelab.adapters.linux_host import LinuxHostAdapter
from app.homelab.adapters.openwrt import OpenWrtAdapter
from app.homelab.adapters.windows_host import WindowsHostAdapter
from app.homelab.health import HomelabProbeLayer, aggregate_state
from app.homelab.history import HomelabHistory
from app.homelab.models import (
    ActionDecision,
    HealthState,
    HostDefinition,
    HostHealth,
    HostType,
    HomelabOverview,
)
from app.homelab.policies import decide, normalize_action
from app.homelab.registry import HomelabHostRegistry
from app.integrations.base import IntegrationError
from app.integrations.home_assistant import HomeAssistantClient
from app.integrations.proxmox.client import ProxmoxReadOnlyClient
from app.tools.shell_approval import ShellApprovalGate
from app.tools.shell_models import ShellRiskLevel
from app.tools.remote_shell import RemoteShellService


logger = logging.getLogger("nyra.homelab")

_PROXMOX_PREFIX = "PROXMOX"


class HomelabControlPlane:
    def __init__(
        self,
        settings: Settings,
        event_bus: EventBus,
        approvals: ShellApprovalGate,
        remote_shell: RemoteShellService,
        history: HomelabHistory | None = None,
        registry: HomelabHostRegistry | None = None,
        proxmox: ProxmoxReadOnlyClient | None = None,
        home_assistant: HomeAssistantClient | None = None,
    ) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.approvals = approvals
        self.remote_shell = remote_shell
        self.history = history or HomelabHistory(settings.database_path)
        self.registry = registry or HomelabHostRegistry(path=settings.homelab_registry_path)
        self.probes = HomelabProbeLayer(
            default_timeout_seconds=settings.homelab_default_timeout_seconds,
            credential_resolver=self._probe_credentials,
        )
        self.proxmox = proxmox or ProxmoxReadOnlyClient(
            settings.proxmox_url,
            settings.proxmox_token_id,
            settings.proxmox_token_secret,
            settings.proxmox_verify_ssl,
            tls_fingerprint=settings.proxmox_tls_fingerprint,
            timeout_seconds=max(4.0, settings.homelab_default_timeout_seconds),
        )
        self.home_assistant = home_assistant or HomeAssistantClient(
            settings.home_assistant_url,
            settings.home_assistant_token,
            enabled=settings.home_assistant_enabled,
            timeout_seconds=max(3.0, settings.homelab_default_timeout_seconds),
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._cache: dict[str, tuple[float, Any]] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._last_emitted_state: dict[str, HealthState] = {}
        self._event_cooldowns: dict[str, float] = {}
        self._semaphore = asyncio.Semaphore(6)
        self._loop_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle

    async def initialize(self) -> None:
        await self.history.initialize()

    def start(self) -> None:
        if not self.settings.homelab_enabled:
            return
        if self._loop_task is None or self._loop_task.done():
            self._stop.clear()
            self._loop_task = asyncio.create_task(self._health_loop(), name="nyra-homelab-control-plane")

    async def stop(self) -> None:
        self._stop.set()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    async def _health_loop(self) -> None:
        interval = max(30, self.settings.homelab_poll_interval)
        while not self._stop.is_set():
            try:
                await self.overview(force=True)
            except Exception as exc:
                logger.warning("homelab_health_loop_failed", extra={"error_type": type(exc).__name__})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    # ------------------------------------------------------------------ helpers

    def _lock_for(self, resource_key: str) -> asyncio.Lock:
        if resource_key not in self._locks:
            self._locks[resource_key] = asyncio.Lock()
        return self._locks[resource_key]

    def _cache_get(self, key: str, ttl: float | None = None) -> tuple[Any, bool]:
        entry = self._cache.get(key)
        effective_ttl = self.settings.homelab_overview_cache_seconds if ttl is None else ttl
        if not entry:
            return None, False
        stored_at, value = entry
        if time.monotonic() - stored_at > effective_ttl:
            return None, False
        return value, True

    def _cache_set(self, key: str, value: Any) -> None:
        self._cache[key] = (time.monotonic(), value)

    @staticmethod
    def _ids() -> tuple[str | None, str | None]:
        return current_turn_id.get(), current_agent_run_id.get()

    def configuration_status(self) -> dict[str, str]:
        def classify(configured: bool, reachable_hint: bool = True) -> str:
            if not configured:
                return "UNCONFIGURED"
            return "READY" if reachable_hint else "DEGRADED"

        return {
            "proxmox": classify(self.proxmox.configured),
            "openwrt": classify(self._ssh_ready("openwrt")),
            # §16 (prompt11_1): token ausente é UNCONFIGURED, nunca READY/DEGRADED.
            "home_assistant": (
                "UNCONFIGURED" if not self.settings.home_assistant_enabled or not self.settings.home_assistant_url
                or self.home_assistant.auth_missing
                else "READY"
            ),
            "windows_dc1": "UNCONFIGURED",
        }

    def _ssh_ready(self, host_id: str) -> bool:
        host = self.registry.get(host_id)
        if not host:
            return False
        logical = self.remote_shell.hosts.resolve_remote(host.id)
        remote = getattr(logical, "remote_shell", None)
        return bool(remote and getattr(remote, "enabled", False))

    def _probe_credentials(self, host: HostDefinition) -> str:
        """Bearer token for reachability probes; empty for every other host."""
        if host.integration.value == "home_assistant_api":
            return self.home_assistant.bearer_token
        return ""

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.homelab_enabled,
            "hosts": self.registry.public_hosts(),
            "configuration": self.configuration_status(),
            "poll_interval": self.settings.homelab_poll_interval,
            "overview_cache_seconds": self.settings.homelab_overview_cache_seconds,
            "default_timeout_seconds": self.settings.homelab_default_timeout_seconds,
        }

    # ------------------------------------------------------------------ health / overview

    async def overview(self, *, force: bool = False) -> HomelabOverview:
        if not force:
            cached, hit = self._cache_get("overview")
            if hit and isinstance(cached, HomelabOverview):
                return cached
        hosts = [h for h in self.registry.all_hosts()]
        results = await asyncio.gather(
            *(self.host_status(h.id, force=True) for h in hosts),
            return_exceptions=True,
        )
        entries: list[HostHealth] = []
        for host, result in zip(hosts, results, strict=True):
            if isinstance(result, BaseException):
                entries.append(HostHealth(
                    host_id=host.id,
                    address=host.address,
                    overall_state=HealthState.UNKNOWN,
                    integration_detail={"error_type": type(result).__name__},
                    observed_at=time.time(),
                ))
            elif isinstance(result, HostHealth):
                entries.append(result)
        summary: dict[str, int] = {}
        for item in entries:
            summary[item.overall_state.value] = summary.get(item.overall_state.value, 0) + 1
        overview = HomelabOverview(generated_at=time.time(), cached=False, hosts=entries, summary=summary)
        self._cache_set("overview", overview)
        await self._emit_transitions(entries)
        return overview

    async def host_status(
        self,
        host_reference: str,
        *,
        force: bool = False,
    ) -> HostHealth:
        host = self.registry.resolve(host_reference)
        if host is None:
            raise IntegrationError("HOMELAB_HOST_UNKNOWN", f"Host não cadastrado no registry: {host_reference}")
        cache_key = f"host:{host.id}"
        if not force:
            cached, hit = self._cache_get(cache_key)
            if hit and isinstance(cached, HostHealth):
                flagged = cached.model_copy(update={"cached": True})
                return flagged
        probes: list = []
        integration_state = HealthState.UNKNOWN
        integration_error: str | None = None
        integration_detail: dict[str, Any] = {}
        if host.enabled:
            probes = await self.probes.probe_host(host)
            integration_state, integration_error, integration_detail = await self._integration_health(host)
        overall, reachable = aggregate_state(host, probes, integration_state, integration_error)
        if host.enabled and not reachable:
            failures = self._consecutive_failures.get(host.id, 0) + 1
            self._consecutive_failures[host.id] = failures
            if failures < self.settings.homelab_offline_failure_threshold:
                previous_cached, _ = self._cache_get(cache_key)
                if isinstance(previous_cached, HostHealth) and previous_cached.reachable:
                    overall = previous_cached.overall_state
                    reachable = previous_cached.reachable
        else:
            self._consecutive_failures[host.id] = 0
        health = HostHealth(
            host_id=host.id,
            address=host.address,
            reachable=bool(reachable),
            overall_state=overall,
            probes=probes,
            integration_state=integration_state,
            integration_error_code=integration_error,
            integration_detail=integration_detail,
            observed_at=time.time(),
        )
        self._cache_set(cache_key, health)
        return health

    async def _integration_health(self, host: HostDefinition) -> tuple[HealthState, str | None, dict[str, Any]]:
        """Query the host's native integration; failures never fake reachability."""
        detail: dict[str, Any] = {}
        try:
            if host.integration.value == "proxmox_api":
                if not self.proxmox.configured:
                    return HealthState.INTEGRATION_UNAVAILABLE, "PROXMOX_AUTH_MISSING", detail
                version = await asyncio.wait_for(self.proxmox.version(), timeout=self.settings.homelab_default_timeout_seconds)
                detail["version"] = str(version.get("version") or "")[:40]
                detail["release"] = str(version.get("release") or "")[:40]
                return HealthState.ONLINE, None, detail
            if host.integration.value == "home_assistant_api":
                # §9 (prompt11_1): sem token NENHUM request sai para /api/ —
                # o monitor não pode gerar "invalid authentication" no HA.
                if self.home_assistant.auth_missing:
                    return HealthState.INTEGRATION_UNAVAILABLE, "HA_AUTH_MISSING", {"token": False}
                root = await asyncio.wait_for(self.home_assistant.api_root(), timeout=self.settings.homelab_default_timeout_seconds)
                config = await self.home_assistant.config()
                detail["api"] = root[:60]
                detail["state"] = str(config.get("state") or "")[:24]
                detail["version"] = str(config.get("version") or "")[:24]
                # prompt11_2 §18/§19: sucesso autenticado do monitor atualiza
                # last_success da fonte única da UI — HA saudável volta a READY
                # em vez de exibir STALE com timestamp antigo.
                try:
                    from app.integrations.home_assistant_profiles import (
                        record_monitor_success,
                    )

                    record_monitor_success(
                        detail=detail,
                        base_url=str(getattr(self.home_assistant, "base_url", "") or ""),
                    )
                except Exception as exc:  # noqa: BLE001 - refresh nunca falha o probe
                    logger.info("ha_monitor_refresh_failed", extra={"error_type": type(exc).__name__})
                return HealthState.ONLINE, None, detail
            if host.integration.value == "trusted_ssh" and host.type == HostType.OPENWRT:
                adapter = OpenWrtAdapter(self.remote_shell, host.id, timeout_seconds=8)
                info = await adapter.run("ubus call system info", timeout_seconds=8, reason="homelab:openwrt:health")
                data = adapter.parse_json_output(info)
                uptime = (data or {}).get("uptime")
                detail["uptime_s"] = float(uptime) if isinstance(uptime, (int, float)) else None
                return HealthState.ONLINE, None, detail
            return HealthState.UNKNOWN, None, detail
        except IntegrationError as exc:
            code = exc.code
            state = (
                HealthState.AUTHENTICATION_FAILED
                if code.endswith(("AUTH_MISSING", "AUTH_FAILED"))
                else HealthState.INTEGRATION_UNAVAILABLE
            )
            return state, code, {"message": exc.message[:200]}
        except SshAdapterError as exc:
            return HealthState.INTEGRATION_UNAVAILABLE, exc.code, {"message": exc.message[:200]}
        except TimeoutError:
            return HealthState.INTEGRATION_UNAVAILABLE, "INTEGRATION_TIMEOUT", {}
        except Exception as exc:
            logger.info("homelab_integration_probe_failed", extra={"host": host.id, "error_type": type(exc).__name__})
            return HealthState.INTEGRATION_UNAVAILABLE, "INTEGRATION_ERROR", {}

    async def _emit_transitions(self, entries: list[HostHealth]) -> None:
        now = time.monotonic()
        cooldown = max(float(self.settings.event_cooldown_seconds), 30.0)
        for item in entries:
            previous = self._last_emitted_state.get(item.host_id)
            if previous == item.overall_state or item.overall_state in {HealthState.UNKNOWN, HealthState.DISABLED}:
                continue
            last_emit = self._event_cooldowns.get(item.host_id, 0.0)
            if now - last_emit < cooldown:
                continue
            self._event_cooldowns[item.host_id] = now
            self._last_emitted_state[item.host_id] = item.overall_state
            event_type = {
                HealthState.ONLINE: EventType.HOMELAB_HOST_ONLINE,
                HealthState.OFFLINE: EventType.HOMELAB_HOST_OFFLINE,
                HealthState.UNREACHABLE: EventType.HOMELAB_HOST_OFFLINE,
                HealthState.AUTHENTICATION_FAILED: EventType.HOMELAB_HOST_DEGRADED,
                HealthState.DEGRADED: EventType.HOMELAB_HOST_DEGRADED,
                HealthState.INTEGRATION_UNAVAILABLE: EventType.HOMELAB_HOST_DEGRADED,
            }.get(item.overall_state)
            if event_type is None:
                continue
            await self.event_bus.publish(
                event_type,
                host=item.host_id,
                address=item.address,
                state=item.overall_state.value,
                integration_error_code=item.integration_error_code,
            )
            try:
                await self.history.add_action(
                    resource=f"host:{item.host_id}",
                    integration="network_probe",
                    action="state_transition",
                    success=True,
                    effect_verified=None,
                    previous_state=(previous.value if previous else None),
                    new_state=item.overall_state.value,
                    detail=f"address={item.address}",
                )
            except Exception as exc:
                logger.info("homelab_history_write_failed", extra={"error_type": type(exc).__name__})

    # ------------------------------------------------------------------ public read API

    def list_hosts(self) -> list[dict]:
        return self.registry.public_hosts()

    async def proxmox_node_status(self, node: str | None = None) -> dict[str, Any]:
        nodes = await self._guarded(self.proxmox.nodes())
        target = node or next((str(n.get("node")) for n in nodes if n.get("status") == "online"), None)
        if not target:
            return {"nodes": [_clean(n) for n in nodes]}
        status = await self._guarded(self.proxmox.node_status(target))
        return {"node": target, **_clean(status)}

    async def proxmox_list_vms(self, include_lxc: bool = True) -> list[dict[str, Any]]:
        resources = await self._guarded(self.proxmox.virtual_machines())
        guests = []
        for item in resources:
            guest_type = "lxc" if str(item.get("type")) == "lxc" else "qemu"
            if guest_type == "lxc" and not include_lxc:
                continue
            guests.append({
                "vmid": item.get("vmid"),
                "name": str(item.get("name") or "")[:80],
                "guest_type": guest_type,
                "node": str(item.get("node") or "")[:40],
                "status": str(item.get("status") or "")[:24],
                "cpu_percent": round(float(item.get("cpu") or 0) * 100, 1),
                "memory_used_bytes": item.get("mem"),
                "memory_total_bytes": item.get("maxmem"),
                "uptime_s": item.get("uptime"),
            })
        guests.sort(key=lambda g: (g["guest_type"], g["vmid"] or 0))
        return guests

    async def proxmox_resolve_guest(self, reference: str) -> dict[str, Any]:
        """Resolve VM/LXC by vmid OR by human name/alias (spec §26)."""
        guests = await self.proxmox_list_vms(include_lxc=True)
        needle = str(reference).strip().casefold()
        direct = None
        if needle.isdigit():
            direct = next((g for g in guests if str(g["vmid"]) == needle), None)
        if direct is None:
            exact = [g for g in guests if str(g["name"]).casefold() == needle]
            partial = [g for g in guests if needle and needle in str(g["name"]).casefold()]
            matches = exact or partial
            if len(matches) == 1:
                direct = matches[0]
            elif len(matches) > 1:
                names = ", ".join(str(m["name"]) for m in matches[:5])
                raise IntegrationError(_PROXMOX_PREFIX + "_VM_NOT_FOUND", f"Referência ambígua para VM: {names}. Especifique o vmid.")
        if direct is None:
            raise IntegrationError(_PROXMOX_PREFIX + "_VM_NOT_FOUND", f"Nenhuma VM/LXC corresponde a {reference!r}.")
        return direct

    async def proxmox_vm_status(self, reference: str) -> dict[str, Any]:
        guest = await self.proxmox_resolve_guest(reference)
        status = await self._guarded(self.proxmox.guest_status(guest["node"], guest["guest_type"], int(guest["vmid"])))
        return {**guest, "detail": _clean(status)}

    async def proxmox_storage_status(self) -> list[dict[str, Any]]:
        storages = await self._guarded(self.proxmox.storage())
        result = []
        for item in storages:
            total = float(item.get("maxdisk") or 0)
            used = float(item.get("disk") or 0)
            result.append({
                "storage": str(item.get("storage") or "")[:64],
                "type": str(item.get("plugintype") or item.get("type") or "")[:32],
                "node": str(item.get("node") or "")[:40],
                "active": bool(item.get("active", True)),
                "enabled": bool(item.get("enabled", True)),
                "total_bytes": total,
                "used_bytes": used,
                "available_bytes": max(total - used, 0.0),
                "usage_percent": round(used * 100.0 / total, 1) if total else None,
            })
        result.sort(key=lambda s: s["storage"])
        return result

    async def proxmox_cluster_status(self) -> list[dict[str, Any]]:
        data = await self._guarded(self.proxmox.cluster_status())
        return [_clean(item) for item in data]

    async def proxmox_recent_tasks(self, limit: int = 15) -> list[dict[str, Any]]:
        nodes = await self._guarded(self.proxmox.nodes())
        target = next((str(n.get("node")) for n in nodes if n.get("status") == "online"), None)
        if not target:
            return []
        tasks = await self._guarded(self.proxmox.recent_tasks(target, limit=min(limit, 50)))
        return [
            {
                "upid": str(t.get("upid") or "")[:80],
                "description": str(t.get("descr") or "")[:120],
                "status": str(t.get("status") or "")[:24],
                "exitstatus": str(t.get("exitstatus") or "")[:40] or None,
                "start_time": t.get("starttime"),
                "end_time": t.get("endtime"),
                "worker_type": str(t.get("type") or "")[:32],
                "guest_vmid": t.get("vmid"),
            }
            for t in tasks
            if isinstance(t, dict)
        ]

    # ------------------------------------------------------------------ proxmox actions

    async def proxmox_vm_action(
        self,
        action: str,
        reference: str,
        *,
        approval_id: str | None = None,
        reason: str = "",
        graceful_timeout: int = 90,
    ) -> dict[str, Any]:
        normalized = normalize_action(action)
        if not normalized:
            return self._action_error(action.upper(), "Ação de VM desconhecida.", risk="ELEVATED")
        guest = await self.proxmox_resolve_guest(reference)
        vmid = int(guest["vmid"])
        resource_key = f"proxmox:{guest['guest_type']}:{vmid}"
        turn_id, agent_run_id = self._ids()
        lock = self._lock_for(resource_key)
        if lock.locked() and agent_run_id is None:
            return self._action_error(normalized.upper(), "Outra ação está em andamento nesta VM.", resource=resource_key, risk="ELEVATED")
        async with lock:
            decision = decide(normalized, self._host_policy_for("proxmox"))
            command_description = f"{normalized} {guest['guest_type']} {vmid} ({guest['name']}) node={guest['node']}"
            if decision.requires_approval:
                approved = await self._require_approval(
                    decision, command_description, resource_key, turn_id, agent_run_id, approval_id,
                )
                if approved is not True:
                    return approved
            previous_status = str(guest.get("status") or "")
            started = time.perf_counter()
            pve_action = normalized.removeprefix("vm_")
            upid = await self._guarded(self.proxmox.guest_action(guest["node"], guest["guest_type"], vmid, pve_action, extra={
                "shutdown": {"timeout": min(graceful_timeout, 180)},
            }.get(normalized)))
            task = await self._guarded(self.proxmox.wait_task(guest["node"], upid, timeout_seconds=max(graceful_timeout + 45, 90)))
            if not task.get("ok"):
                error_code = "PROXMOX_TASK_FAILED"
                message = f"A tarefa do Proxmox não concluiu com sucesso ({task.get('exitstatus') or 'timeout'})."
                await self.event_bus.publish(EventType.PROXMOX_TASK_FAILED, vmid=vmid, guest=guest["name"], action=normalized, exitstatus=str(task.get("exitstatus") or ""))
                await self.history.add_action(resource=resource_key, integration="proxmox", action=normalized, success=False, effect_verified=False, error_code=error_code, agent_run_id=agent_run_id, turn_id=turn_id, previous_state=previous_status, detail=command_description)
                return self._action_error(error_code, message, resource=resource_key, risk=decision.risk_level, turn_id=turn_id, agent_run_id=agent_run_id)
            await self.event_bus.publish(EventType.PROXMOX_TASK_COMPLETED, vmid=vmid, guest=guest["name"], action=normalized)
            expected = {
                "vm_start": "running",
                "vm_shutdown": "stopped",
                "vm_stop": "stopped",
                "vm_reboot": "running",
                "vm_reset": "running",
            }[normalized]
            fresh = await self._guarded(self.proxmox.guest_status(guest["node"], guest["guest_type"], vmid))
            actual = str(fresh.get("status") or "")
            effect_verified = actual == expected
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            await self.event_bus.publish(EventType.PROXMOX_VM_CHANGED, vmid=vmid, guest=guest["name"], action=normalized, previous_state=previous_status, new_state=actual, effect_verified=effect_verified)
            await self.history.add_action(
                resource=resource_key, integration="proxmox", action=normalized,
                success=True, effect_verified=effect_verified,
                agent_run_id=agent_run_id, turn_id=turn_id,
                previous_state=previous_status, new_state=actual,
                detail=f"upid={upid[:60]} exit={task.get('exitstatus')}",
            )
            return {
                "success": True,
                "execution_id": upid.split(":")[-1][:48],
                "tool": f"proxmox_{normalized}",
                "risk_level": decision.risk_level,
                "resource": resource_key,
                "vmid": vmid,
                "guest_type": guest["guest_type"],
                "name": guest["name"],
                "node": guest["node"],
                "task_state": task.get("state"),
                "task_exitstatus": task.get("exitstatus"),
                "previous_state": previous_status,
                "guest_status": actual,
                "expected_status": expected,
                "effect_verified": effect_verified,
                "verification_status": "VERIFIED" if effect_verified else "VERIFICATION_FAILED",
                "duration_ms": duration_ms,
                "turn_id": turn_id,
                "agent_run_id": agent_run_id,
            }

    async def _require_approval(
        self,
        decision: ActionDecision,
        description: str,
        resource_key: str,
        turn_id: str | None,
        agent_run_id: str | None,
        approval_id: str | None,
    ) -> Any:
        timeout_int = max(5, int(self.settings.ssh_command_timeout_seconds))
        fingerprint = self.approvals.fingerprint(
            description, "homelab", "", timeout_int, target=resource_key, agent_run_id=agent_run_id,
        )
        if approval_id:
            granted, rejection = self.approvals.consume(approval_id, fingerprint)
            if not granted:
                return self._action_error(
                    "APPROVAL_REJECTED", rejection or "A aprovação não é válida para esta ação.",
                    resource=resource_key, risk=decision.risk_level, approval_id=approval_id,
                    turn_id=turn_id, agent_run_id=agent_run_id,
                )
            return True
        record = self.approvals.request(
            command=description,
            shell="homelab",
            working_directory="",
            timeout_seconds=timeout_int,
            risk_level=ShellRiskLevel(decision.risk_level),
            target=resource_key,
            agent_run_id=agent_run_id,
        )
        await self.event_bus.publish(
            EventType.SHELL_APPROVAL_REQUIRED,
            approval_id=record.approval_id,
            agent_run_id=agent_run_id,
            command=description,
            shell="homelab",
            risk_level=decision.risk_level,
            target=resource_key,
            reason=decision.reason,
            turn_id=turn_id,
        )
        logger.info("homelab_approval_required", extra={
            "approval_id": record.approval_id, "target": resource_key,
            "risk": decision.risk_level, "turn_id": turn_id,
        })
        payload = self._action_error(
            "APPROVAL_REQUIRED", f"Esta ação requer autorização explícita. {decision.reason}",
            resource=resource_key, risk=decision.risk_level, approval_id=record.approval_id,
            turn_id=turn_id, agent_run_id=agent_run_id,
        )
        payload["approval_required"] = True
        payload["command"] = description
        return payload

    def _host_policy_for(self, host_id: str) -> dict:
        host = self.registry.get(host_id)
        return host.risk_policy if host else {}

    # ------------------------------------------------------------------ home assistant

    async def ha_status(self) -> dict[str, Any]:
        if not self.settings.home_assistant_enabled:
            return {"enabled": False, "state": "DISABLED"}
        if not self.settings.home_assistant_url:
            return {"enabled": True, "configured": False, "error_code": "HA_AUTH_MISSING", "message": "URL do Home Assistant não configurada."}
        if self.home_assistant.auth_missing:
            return {"enabled": True, "configured": True, "error_code": "HA_AUTH_MISSING", "message": "Token de acesso não configurado."}
        root = await self._guarded(self.home_assistant.api_root())
        config = await self._guarded(self.home_assistant.config())
        states = await self._guarded(self.home_assistant.states())
        return {
            "enabled": True,
            "configured": True,
            "api_response": root[:80],
            "location_name": str(config.get("location_name") or "")[:80],
            "state": str(config.get("state") or "")[:24],
            "version": str(config.get("version") or "")[:24],
            "time_zone": str(config.get("time_zone") or "")[:40],
            "entity_count": len(states),
            "url_base_path_only": True,
        }

    async def ha_list_entities(
        self,
        domain: str | None = None,
        state: str | None = None,
        search: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        states = await self._guarded(self.home_assistant.states())
        domain_filter = (domain or "").strip().casefold().rstrip(".")
        state_filter = (state or "").strip().casefold()
        needle = (search or "").strip().casefold()
        bounded = max(1, min(limit, 100))
        results = []
        for entity in states:
            entity_id = str(entity.get("entity_id") or "")
            if domain_filter and not entity_id.startswith(f"{domain_filter}."):
                continue
            entity_state = str(entity.get("state") or "").casefold()
            if state_filter and entity_state != state_filter:
                continue
            if needle and needle not in entity_id.casefold() and needle not in json_dumps_attrs(entity.get("attributes")):
                continue
            attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
            results.append({
                "entity_id": entity_id[:120],
                "state": entity_state[:60],
                "friendly_name": str(attributes.get("friendly_name") or "")[:100],
                "domain": entity_id.split(".", 1)[0] if "." in entity_id else "",
                "last_changed": str(entity.get("last_changed") or "")[:40],
                "last_updated": str(entity.get("last_updated") or "")[:40],
            })
            if len(results) >= bounded:
                break
        return results

    async def ha_get_state(self, entity_id: str) -> dict[str, Any]:
        entity = await self._guarded(self.home_assistant.state(entity_id))
        attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
        limited_attributes = {str(k)[:60]: _short(v) for k, v in list(attributes.items())[:20]}
        return {
            "entity_id": str(entity.get("entity_id") or entity_id)[:120],
            "state": str(entity.get("state") or "")[:80],
            "attributes": limited_attributes,
            "last_changed": str(entity.get("last_changed") or "")[:40],
            "last_updated": str(entity.get("last_updated") or "")[:40],
        }

    async def ha_call_service(
        self,
        domain: str,
        service: str,
        target: dict[str, Any] | None = None,
        service_data: dict[str, Any] | None = None,
        *,
        approval_id: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        safe_domain = (domain or "").strip().casefold()
        safe_service = (service or "").strip().casefold()
        if not re_ok_domain(safe_domain) or not re_ok_domain(safe_service):
            return self._action_error("HA_SERVICE_FAILED", "Domínio ou serviço inválido.", risk="ELEVATED")
        primary_entity = ""
        if isinstance(target, dict):
            values = target.get("entity_id")
            items = values if isinstance(values, list) else ([values] if isinstance(values, str) else [])
            primary_entity = next((str(v) for v in items if v), "")
        resource_key = f"ha:{primary_entity or safe_domain}.{safe_service}"
        decision = decide("ha_call_service", self._host_policy_for("home_assistant"))
        if not _HA_SAFE_SERVICES.match(f"{safe_domain}.{safe_service}"):
            decision = ActionDecision(action="ha_call_service", risk_level="ELEVATED", requires_approval=True, reason="Serviço fora da allowlist de baixo risco.")
        turn_id, agent_run_id = self._ids()
        lock = self._lock_for(resource_key)
        async with lock:
            if decision.requires_approval:
                description = f"ha_call_service {safe_domain}.{safe_service}" + (f" target={primary_entity}" if primary_entity else "")
                approved = await self._require_approval(decision, description, resource_key, turn_id, agent_run_id, approval_id)
                if approved is not True:
                    return approved
            await self._guarded(self.home_assistant.call_service(safe_domain, safe_service, target, service_data))
            effect_verified: bool | None = None
            expected_state = service_data.pop("_expected_state", None) if isinstance(service_data, dict) else None
            if primary_entity and isinstance(expected_state, str):
                effect_verified = await self.home_assistant.verify_effect(primary_entity, expected_state)
            elif primary_entity and safe_service in {"turn_on", "turn_off", "toggle"}:
                fresh = await self.ha_get_state(primary_entity)
                new_state = str(fresh.get("state") or "").casefold()
                if new_state in {"on", "off"}:
                    effect_verified = new_state != "" and (new_state == ("on" if safe_service == "turn_on" else "off"))
            verified_payload = {
                "success": True,
                "tool": "ha_call_service",
                "risk_level": decision.risk_level,
                "resource": resource_key,
                "domain": safe_domain,
                "service": safe_service,
                "target_entity": primary_entity[:120] or None,
                "effect_verified": effect_verified,
            }
            if effect_verified is None:
                verified_payload["verification_status"] = "EXECUTED"
                verified_payload["note"] = "Service call aceito pelo Home Assistant; efeito não confirmado automaticamente."
            else:
                verified_payload["verification_status"] = "VERIFIED" if effect_verified else "VERIFICATION_FAILED"
                await self.event_bus.publish(
                    EventType.HOME_ASSISTANT_ACTION_VERIFIED,
                    entity_id=primary_entity, domain=safe_domain, service=safe_service,
                    effect_verified=effect_verified,
                )
            await self.history.add_action(
                resource=resource_key, integration="home_assistant", action=f"{safe_domain}.{safe_service}",
                success=True, effect_verified=effect_verified,
                agent_run_id=agent_run_id, turn_id=turn_id,
                new_state=(str(expected_state) if expected_state else None),
                detail=reason[:200],
            )
            return verified_payload

    # ------------------------------------------------------------------ ssh adapters

    def _adapter_for(self, host: HostDefinition) -> Any:
        if host.type == HostType.OPENWRT:
            return OpenWrtAdapter(self.remote_shell, host.id)
        if host.type == HostType.LINUX or host.type == HostType.PROXMOX:
            return LinuxHostAdapter(self.remote_shell, host.id)
        if host.type == HostType.WINDOWS:
            method = str(host.metadata.get("remote_method") or "unconfigured")
            adapter = WindowsHostAdapter(self.remote_shell, host.id)
            adapter.remote_method = method
            return adapter
        raise SshAdapterErrorLike("CAPABILITY_UNAVAILABLE", "Este host não possui adapter SSH.")

    async def openwrt_status(self) -> dict[str, Any]:
        return await self._run_adapter_read("openwrt", lambda a: a.status(), cache_key="openwrt:status")

    async def openwrt_interfaces(self) -> dict[str, Any]:
        return await self._run_adapter_read("openwrt", lambda a: a.interfaces(), cache_key="openwrt:interfaces")

    async def openwrt_wifi_status(self) -> dict[str, Any]:
        return await self._run_adapter_read("openwrt", lambda a: a.wifi_status())

    async def openwrt_logs(self, lines: int = 30) -> dict[str, Any]:
        return await self._run_adapter_read("openwrt", lambda a: a.logs(lines))

    async def host_metrics(self, host_reference: str) -> dict[str, Any]:
        host = self.registry.resolve(host_reference)
        if host is None:
            raise IntegrationError("HOMELAB_HOST_UNKNOWN", f"Host não cadastrado: {host_reference}")
        if host.type == HostType.OPENWRT:
            adapter = OpenWrtAdapter(self.remote_shell, host.id)
            return {"host": host.id, **await adapter.status()}
        adapter = self._adapter_for(host)
        if isinstance(adapter, WindowsHostAdapter):
            ok, message = adapter.available()
            if not ok:
                return {"host": host.id, "success": False, "error_code": "CAPABILITY_UNAVAILABLE", "message": message}
        return {"host": host.id, **await self._guarded(adapter.metrics())}

    async def host_services(self, host_reference: str, limit: int = 50) -> dict[str, Any]:
        host = self.registry.resolve(host_reference)
        if host is None:
            raise IntegrationError("HOMELAB_HOST_UNKNOWN", f"Host não cadastrado: {host_reference}")
        if host.type == HostType.OPENWRT:
            raise SshAdapterErrorLike("CAPABILITY_UNAVAILABLE", "Listagem systemd não se aplica ao OpenWrt; use openwrt_status.")
        adapter = self._adapter_for(host)
        services = await self._guarded(adapter.services(limit))
        return {"host": host.id, **services}

    async def _run_adapter_read(self, host_id: str, operation, cache_key: str | None = None) -> dict[str, Any]:
        if cache_key:
            cached, hit = self._cache_get(cache_key, ttl=10.0)
            if hit:
                return {**cached, "cached": True}
        host = self.registry.get(host_id)
        if host is None:
            raise IntegrationError("HOMELAB_HOST_UNKNOWN", f"Host não cadastrado: {host_id}")
        adapter = self._adapter_for(host)
        payload = await self._guarded(operation(adapter))
        if cache_key:
            self._cache_set(cache_key, payload)
        return payload

    # ------------------------------------------------------------------ error plumbing

    async def _guarded(self, coro):
        return await coro

    @staticmethod
    def _action_error(
        code: str,
        message: str,
        *,
        resource: str = "",
        risk: str = "ELEVATED",
        approval_id: str | None = None,
        turn_id: str | None = None,
        agent_run_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "success": False,
            "error_code": code,
            "message": message,
            "risk_level": risk,
            "resource": resource,
            "approval_id": approval_id,
            "turn_id": turn_id,
            "agent_run_id": agent_run_id,
        }


_HA_SAFE_SERVICES = re.compile(r"^(light|switch|input_boolean|scene|automation|script|media_player)\.(turn_on|turn_off|toggle|run_scene|trigger)$")


def re_ok_domain(value: str) -> bool:
    import re as _re

    return bool(_re.fullmatch(r"[a-z0-9_]{1,64}", value))


def json_dumps_attrs(attributes: Any) -> str:
    try:
        import json as _json

        return _json.dumps(attributes, ensure_ascii=False)[:2000].casefold()
    except (TypeError, ValueError):
        return ""


def _clean(data: dict) -> dict:
    """Project raw API dicts into bounded JSON-safe structures."""
    cleaned: dict = {}
    for key, value in list(data.items())[:60]:
        if isinstance(value, (str, int, float, bool)) or value is None:
            cleaned[key] = value if not isinstance(value, str) else value[:200]
        elif isinstance(value, dict):
            cleaned[key] = _clean(value)
        elif isinstance(value, list):
            cleaned[key] = [
                _clean(item) if isinstance(item, dict) else (item[:200] if isinstance(item, str) else item)
                for item in value[:40]
            ]
    return cleaned


def _short(value: Any, limit: int = 200) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return json_dumps_attrs(value)[:limit]
