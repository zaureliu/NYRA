"""Homelab Control Plane tools registered into the shared ToolRegistry.

Read-only observation tools are READ_ONLY; mutations declare their policy risk
and expose the same approval_id contract as remote_shell (APPROVAL_REQUIRED +
single-use ShellApprovalGate records), so the existing Agent Loop approval UX,
resource locking and grounding work unchanged (spec §78-§88, §182).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from app.homelab.adapters.base import SshAdapterError
from app.homelab.controller import HomelabControlPlane
from app.homelab.policies import decide
from app.integrations.base import IntegrationError
from app.tools.models import EmptyInput, RiskLevel, ToolResult
from app.tools.registry import ToolDefinition


_HA_SAFE = re.compile(r"^(light|switch|input_boolean|scene|automation|script|media_player)\.(turn_on|turn_off|toggle|run_scene|trigger)$")


class HomelabHostRefInput(BaseModel):
    host: str = Field(min_length=1, max_length=120, description="Host por id ou alias do registry (ex.: proxmox, openwrt, dc1).")


class ProxmoxNodeInput(BaseModel):
    node: str | None = Field(default=None, max_length=80)


class ProxmoxVmsInput(BaseModel):
    include_lxc: bool = Field(default=True, description="Incluir containers LXC junto às VMs QEMU.")


class ProxmoxVmRefInput(BaseModel):
    vm: str = Field(min_length=1, max_length=120, description="VMID ou nome/alias da VM/container.")


class ProxmoxVmActionInput(ProxmoxVmRefInput):
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)
    reason: str = Field(default="", max_length=300)


class ProxmoxTasksInput(BaseModel):
    limit: int = Field(default=15, ge=1, le=50)


class HaListEntitiesInput(BaseModel):
    domain: str | None = Field(default=None, max_length=64, description="Filtro por domínio (ex.: light, sensor).")
    state: str | None = Field(default=None, max_length=64)
    search: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=25, ge=1, le=100)


class HaEntityInput(BaseModel):
    entity_id: str = Field(min_length=3, max_length=140, pattern=r"^[A-Za-z0-9_.\-]+$")


class HaServiceCallInput(BaseModel):
    domain: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    service: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    target: dict[str, Any] | None = Field(default=None, description="Alvo estruturado (entity_id/device_id/area_id).")
    service_data: dict[str, Any] | None = Field(default=None, description="Dados do serviço (ex.: brightness).")
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)
    reason: str = Field(default="", max_length=300)


class OpenWrtLogsInput(BaseModel):
    lines: int = Field(default=30, ge=1, le=120)


class HostServicesInput(HomelabHostRefInput):
    limit: int = Field(default=50, ge=1, le=120)


def register_homelab_tools(registry, control_plane: HomelabControlPlane) -> None:
    """Register all Homelab Control Plane capabilities."""

    def guarded(async_fn):
        async def wrapper(*args, **kwargs) -> dict[str, Any]:
            try:
                return await async_fn(*args, **kwargs)
            except IntegrationError as exc:
                return {"success": False, "error_code": exc.code, "message": exc.message}
            except SshAdapterError as exc:
                return {"success": False, "error_code": exc.code, "message": exc.message}
            except TimeoutError:
                return {"success": False, "error_code": "HOMELAB_TIMEOUT", "message": "A consulta ao homelab excedeu o timeout."}
        return wrapper

    def vm_action_tool(action: str):
        decision = decide(action)

        def preflight(payload: dict[str, Any]) -> dict[str, Any]:
            vm = str(payload.get("vm") or "").strip().casefold()
            normalized = re.sub(r"\s+", "-", vm)[:60] or "unknown"
            return {
                "risk_level": decision.risk_level,
                "resource_key": f"proxmox:guest:{normalized}",
            }

        async def run(vm: str, approval_id: str | None = None, reason: str = "") -> dict[str, Any]:
            result = await control_plane.proxmox_vm_action(
                action, vm, approval_id=approval_id, reason=reason,
            )
            if not result.get("success") and result.get("error_code") == "PROXMOX_TASK_FAILED":
                result["effect_verified"] = False
            return result

        run.__name__ = action
        definition = ToolDefinition(
            name=f"proxmox_{action}",
            description=_ACTION_DESCRIPTIONS[action],
            risk=RiskLevel[decision.risk_level],
            input_model=ProxmoxVmActionInput,
            function=guarded(run),
            preflight=preflight,
            llm_enabled=control_plane.settings.homelab_mutations_enabled,
        )
        registry.register(definition)

    # ---------------- overview & hosts

    async def homelab_overview() -> dict[str, Any]:
        overview = await control_plane.overview()
        return overview.model_dump(mode="json")

    async def homelab_list_hosts() -> dict[str, Any]:
        hosts = control_plane.list_hosts()
        return {"hosts": hosts, "configuration": control_plane.configuration_status()}

    async def homelab_host_status(host: str) -> dict[str, Any]:
        health = await control_plane.host_status(host)
        return health.model_dump(mode="json")

    registry.register(ToolDefinition(
        "homelab_overview",
        "Verifica o homelab inteiro de uma vez: estado normalizado de cada host (OpenWrt, Proxmox, Home Assistant, DC1) com probes ICMP/TCP/HTTP e saúde das integrações.",
        RiskLevel.READ_ONLY, EmptyInput, guarded(homelab_overview),
    ))
    registry.register(ToolDefinition(
        "homelab_list_hosts",
        "Lista os hosts cadastrados no Unified Host Registry com aliases, endereços, integração e configuração.",
        RiskLevel.READ_ONLY, EmptyInput, guarded(homelab_list_hosts),
    ))
    registry.register(ToolDefinition(
        "homelab_host_status",
        "Status atual e detalhado de um host específico do homelab (por id ou alias), correlacionando probes de rede com a integração nativa.",
        RiskLevel.READ_ONLY, HomelabHostRefInput, guarded(homelab_host_status),
    ))

    # ---------------- proxmox reads

    async def proxmox_node_status(node: str | None = None) -> dict[str, Any]:
        return {"success": True, **await control_plane.proxmox_node_status(node)}

    async def proxmox_list_vms(include_lxc: bool = True) -> dict[str, Any]:
        guests = await control_plane.proxmox_list_vms(include_lxc)
        running = sum(1 for g in guests if g["status"] == "running")
        return {"success": True, "count": len(guests), "running_count": running, "vms": guests}

    async def proxmox_vm_status(vm: str) -> dict[str, Any]:
        return {"success": True, **await control_plane.proxmox_vm_status(vm)}

    async def proxmox_storage_status() -> dict[str, Any]:
        storages = await control_plane.proxmox_storage_status()
        return {"success": True, "storage": storages}

    async def proxmox_cluster_status() -> dict[str, Any]:
        nodes = await control_plane.proxmox_cluster_status()
        return {"success": True, "cluster": nodes}

    async def proxmox_recent_tasks(limit: int = 15) -> dict[str, Any]:
        tasks = await control_plane.proxmox_recent_tasks(limit)
        return {"success": True, "tasks": tasks}

    registry.register(ToolDefinition(
        "proxmox_node_status",
        "Lê status real do nó Proxmox via API nativa: uptime, CPU, load, memória, swap, rootfs e versão do kernel/PVE.",
        RiskLevel.READ_ONLY, ProxmoxNodeInput, guarded(proxmox_node_status),
    ))
    registry.register(ToolDefinition(
        "proxmox_list_vms",
        "Lista VMs QEMU e containers LXC reais do Proxmox via API (vmid, nome, tipo, node, status, CPU, memória, uptime).",
        RiskLevel.READ_ONLY, ProxmoxVmsInput, guarded(proxmox_list_vms),
    ))
    registry.register(ToolDefinition(
        "proxmox_vm_status",
        "Status atual de uma VM/LXC específica do Proxmox por vmid ou nome.",
        RiskLevel.READ_ONLY, ProxmoxVmRefInput, guarded(proxmox_vm_status),
    ))
    registry.register(ToolDefinition(
        "proxmox_storage_status",
        "Lê storages do Proxmox com uso, capacidade, percentual e estado ativo.",
        RiskLevel.READ_ONLY, EmptyInput, guarded(proxmox_storage_status),
    ))
    registry.register(ToolDefinition(
        "proxmox_cluster_status",
        "Lê status do cluster Proxmox (funciona também para node único).",
        RiskLevel.READ_ONLY, EmptyInput, guarded(proxmox_cluster_status),
    ))
    registry.register(ToolDefinition(
        "proxmox_recent_tasks",
        "Consulta tarefas recentes do Proxmox com estado e exit status.",
        RiskLevel.READ_ONLY, ProxmoxTasksInput, guarded(proxmox_recent_tasks),
    ))

    # ---------------- proxmox actions

    for action in ("vm_start", "vm_shutdown", "vm_stop", "vm_reboot", "vm_reset"):
        vm_action_tool(action)

    # ---------------- home assistant

    async def ha_status() -> dict[str, Any]:
        return {**await control_plane.ha_status(), "success": True}

    async def ha_list_entities(domain: str | None = None, state: str | None = None, search: str | None = None, limit: int = 25) -> dict[str, Any]:
        entities = await control_plane.ha_list_entities(domain, state, search, limit)
        return {"success": True, "count": len(entities), "entities": entities}

    async def ha_get_state(entity_id: str) -> dict[str, Any]:
        return {"success": True, **await control_plane.ha_get_state(entity_id)}

    def ha_service_preflight(payload: dict[str, Any]) -> dict[str, Any]:
        domain = str(payload.get("domain") or "").strip().casefold()
        service = str(payload.get("service") or "").strip().casefold()
        target = payload.get("target") or {}
        entity = ""
        values = target.get("entity_id") if isinstance(target, dict) else None
        items = values if isinstance(values, list) else ([values] if isinstance(values, str) else [])
        entity = next((str(v).split(".")[-1][:40] for v in items if v), domain)
        risk = (
            "LOW_RISK"
            if _HA_SAFE.fullmatch(f"{domain}.{service}")
            else "ELEVATED"
        )
        requires_approval = True
        return {
            "risk_level": risk,
            "resource_key": f"ha:{entity}.{service}",
            "requires_approval_override": requires_approval,
        }

    async def ha_call_service(
        domain: str,
        service: str,
        target: dict[str, Any] | None = None,
        service_data: dict[str, Any] | None = None,
        approval_id: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        return await control_plane.ha_call_service(
            domain, service, target, service_data, approval_id=approval_id, reason=reason,
        )

    registry.register(ToolDefinition(
        "ha_status",
        "Verifica o Home Assistant pela REST API real: API running, Core state, versão, location_name, timezone e contagem de entidades.",
        RiskLevel.READ_ONLY, EmptyInput, guarded(ha_status),
    ))
    registry.register(ToolDefinition(
        "ha_list_entities",
        "Lista entidades reais do Home Assistant com filtros por domínio, estado e busca textual.",
        RiskLevel.READ_ONLY, HaListEntitiesInput, guarded(ha_list_entities),
    ))
    registry.register(ToolDefinition(
        "ha_get_state",
        "Lê o estado atual e atributos limitados de uma entidade do Home Assistant.",
        RiskLevel.READ_ONLY, HaEntityInput, guarded(ha_get_state),
    ))
    registry.register(ToolDefinition(
        "ha_call_service",
        "Executa um service call no Home Assistant (ex.: light.turn_off em entity alvo) e tenta verificar o efeito lendo o estado depois. Serviços fora da allowlist exigem approval_id.",
        RiskLevel.LOW_RISK, HaServiceCallInput, guarded(ha_call_service),
        dynamic_risk=True,
        preflight=ha_service_preflight,
        llm_enabled=control_plane.settings.homelab_mutations_enabled,
    ))

    # ---------------- OpenWrt

    async def openwrt_status() -> dict[str, Any]:
        data = await control_plane.openwrt_status()
        return {"success": True, "host": "openwrt", **data}

    async def openwrt_interfaces() -> dict[str, Any]:
        data = await control_plane.openwrt_interfaces()
        return {"success": True, "host": "openwrt", **data}

    async def openwrt_wifi_status() -> dict[str, Any]:
        data = await control_plane.openwrt_wifi_status()
        return {"success": True, "host": "openwrt", **data}

    async def openwrt_logs(lines: int = 30) -> dict[str, Any]:
        data = await control_plane.openwrt_logs(lines)
        return {"success": True, "host": "openwrt", **data}

    registry.register(ToolDefinition(
        "openwrt_status",
        "Status estruturado do gateway OpenWrt via Trusted SSH: uptime, load, memória, WAN, LAN, rota padrão e versão.",
        RiskLevel.READ_ONLY, EmptyInput, guarded(openwrt_status),
    ))
    registry.register(ToolDefinition(
        "openwrt_interfaces",
        "Lista interfaces de rede do OpenWrt (estado, protocolo, endereços, dispositivo).",
        RiskLevel.READ_ONLY, EmptyInput, guarded(openwrt_interfaces),
    ))
    registry.register(ToolDefinition(
        "openwrt_wifi_status",
        "Estado do rádio Wi-Fi do OpenWrt via ubus network.wireless.",
        RiskLevel.READ_ONLY, EmptyInput, guarded(openwrt_wifi_status),
    ))
    registry.register(ToolDefinition(
        "openwrt_logs",
        "Últimas linhas do logread do OpenWrt (somente leitura, limitadas).",
        RiskLevel.READ_ONLY, OpenWrtLogsInput, guarded(openwrt_logs),
    ))

    # ---------------- generic host metrics/services

    async def host_metrics(host: str) -> dict[str, Any]:
        return {**await control_plane.host_metrics(host), "success": True}

    async def host_services(host: str, limit: int = 50) -> dict[str, Any]:
        return {**await control_plane.host_services(host, limit), "success": True}

    registry.register(ToolDefinition(
        "host_metrics",
        "Métricas normalizadas de um host Linux/OpenWrt/Windows registrado (CPU/load, memória, storage, uptime) pelo adapter correto.",
        RiskLevel.READ_ONLY, HomelabHostRefInput, guarded(host_metrics),
    ))
    registry.register(ToolDefinition(
        "host_services",
        "Lista serviços/systemd units de um host Linux registrado (somente leitura).",
        RiskLevel.READ_ONLY, HostServicesInput, guarded(host_services),
    ))


_ACTION_DESCRIPTIONS = {
    "vm_start": "Inicia uma VM/LXC no Proxmox via API nativa; aguarda a tarefa (UPID) terminar e verifica o estado do guest antes de reportar.",
    "vm_shutdown": "Desliga graciosamente uma VM/LXC do Proxmox; aguarda task e verifica estado. Requer confirmação do operador.",
    "vm_stop": "Hard stop imediato de uma VM/LXC no Proxmox (risco alto); aguarda task e verifica estado. Requer aprovação explícita.",
    "vm_reboot": "Reinicia uma VM/LXC no Proxmox; aguarda task e verifica estado. Requer confirmação do operador.",
    "vm_reset": "Reset forçado (equivalente a botão de power) de uma VM no Proxmox; impacto elevado e aprovação obrigatória.",
}
