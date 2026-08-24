"""Runtime Supervisor native tools: structured alternatives to shell for registered services."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.agent.context import current_agent_run_id
from app.core.paths import PROJECT_ROOT
from app.tools.models import RiskLevel
from app.tools.registry import ToolDefinition
from app.tools.shell_approval import ShellApprovalGate
from app.tools.shell_models import ShellRiskLevel

if TYPE_CHECKING:
    from app.events import EventBus
    from app.runtime.supervisor import RuntimeSupervisor


class RuntimeStatusInput(BaseModel):
    service: str = Field(default="", max_length=64, pattern=r"^[a-z0-9_]*$")


class RuntimeServiceInput(BaseModel):
    service: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_]+$")
    approval_id: str | None = Field(
        default=None, min_length=16, max_length=128,
        description="ID de aprovação emitido pelo backend para esta ação exata neste serviço.",
    )
    reason: str = Field(default="", max_length=500)


class RuntimeLogsInput(RuntimeServiceInput):
    lines: int = Field(default=100, ge=1, le=1000)


_ACTION_RISK_FIELD = {"start": "start", "stop": "stop", "restart": "restart"}


def _risk_for(supervisor: "RuntimeSupervisor", service: str, action: str) -> tuple[RiskLevel, object | None]:
    spec = supervisor.registry.get(service)
    if spec is None:
        return RiskLevel.ELEVATED, None
    raw = getattr(spec.risk, _ACTION_RISK_FIELD.get(action, "status"), "READ_ONLY")
    try:
        return RiskLevel(str(raw)), spec
    except ValueError:
        return RiskLevel.ELEVATED, spec


def _mutation_preflight(supervisor: "RuntimeSupervisor", action: str):
    def preflight(payload: dict) -> dict:
        risk, _spec = _risk_for(supervisor, payload.get("service", ""), action)
        return {"risk_level": risk.value, "resource_key": f"runtime:{payload.get('service', '')}", "host": "local"}
    return preflight


async def _mutate(
    supervisor: "RuntimeSupervisor",
    approvals: ShellApprovalGate,
    event_bus: "EventBus",
    action: str,
    service: str,
    approval_id: str | None,
) -> dict:
    spec = supervisor.registry.get(service)
    if spec is None:
        error = supervisor.registry.error_for(service)
        return {
            "success": False, "service": service, "action": action,
            "error_code": "INVALID_CONFIGURATION" if error else "UNKNOWN_SERVICE",
            "message": error or "Serviço não registrado no Runtime Registry.",
        }
    risk_level, resolved_spec = _risk_for(supervisor, service, action)

    if risk_level in {RiskLevel.ELEVATED, RiskLevel.DESTRUCTIVE, RiskLevel.CRITICAL}:
        agent_run_id = current_agent_run_id.get()
        fingerprint = ShellApprovalGate.fingerprint(
            f"runtime_{action} {service}", "runtime",
            str(resolved_spec.working_directory or PROJECT_ROOT),
            int(resolved_spec.startup_timeout_seconds),
            target=f"runtime:{service}", agent_run_id=agent_run_id,
        )
        if approval_id:
            granted, rejection = approvals.consume(approval_id, fingerprint)
            if not granted:
                return {
                    "success": False, "service": service, "action": action,
                    "error_code": "COMMAND_REJECTED", "message": rejection,
                }
        else:
            record = approvals.request(
                command=f"runtime_{action} {service}",
                shell="runtime",
                working_directory=str(resolved_spec.working_directory or PROJECT_ROOT),
                timeout_seconds=int(resolved_spec.startup_timeout_seconds),
                risk_level=ShellRiskLevel(risk_level.value),
                target=f"runtime:{service}",
                agent_run_id=agent_run_id,
            )
            from app.events import EventType

            await event_bus.publish(
                EventType.SHELL_APPROVAL_REQUIRED,
                approval_id=record.approval_id, agent_run_id=agent_run_id,
                command=f"runtime_{action} {service}", shell="runtime",
                working_directory=str(resolved_spec.working_directory or PROJECT_ROOT),
                risk_level=risk_level.value,
                reason=f"runtime {action} em serviço registrado",
            )
            return {
                "success": False, "service": service, "action": action,
                "state": supervisor._state_of(service).value,
                "error_code": "APPROVAL_REQUIRED",
                "message": "Esta ação exige autorização explícita e vinculada antes da execução.",
                "approval_required": True,
                "approval_id": record.approval_id,
                "execution_success": False, "effect_verified": False,
                "verification_status": "EXECUTION_FAILED",
            }

    method = getattr(supervisor, action)
    return await method(service, origin="tool", approval_id=approval_id)


def register_runtime_tools(registry, supervisor: "RuntimeSupervisor", approvals: ShellApprovalGate) -> None:
    event_bus = supervisor.event_bus

    async def runtime_all() -> dict:
        snapshots = [snapshot.model_dump(mode="json") for snapshot in await supervisor.inspect_all_public()]
        return {"success": True, "services": snapshots, "count": len(snapshots), "verification_status": "VERIFIED"}

    async def runtime_one(service: str) -> dict:
        snapshot = supervisor._snapshot(service)
        if snapshot is None:
            return {"success": False, "service": service, "error_code": "UNKNOWN_SERVICE"}
        fresh = await supervisor.inspect(service)
        return {"success": True, "verification_status": "VERIFIED", **fresh.model_dump(mode="json")}

    async def runtime_health(service: str) -> dict:
        return await supervisor.health(service)

    async def runtime_logs(service: str, lines: int = 100) -> dict:
        return await supervisor.logs(service, lines=lines)

    async def mutate_start(service: str, approval_id: str | None = None, reason: str = "") -> dict:
        return await _mutate(supervisor, approvals, event_bus, "start", service, approval_id)

    async def mutate_stop(service: str, approval_id: str | None = None, reason: str = "") -> dict:
        return await _mutate(supervisor, approvals, event_bus, "stop", service, approval_id)

    async def mutate_restart(service: str, approval_id: str | None = None, reason: str = "") -> dict:
        return await _mutate(supervisor, approvals, event_bus, "restart", service, approval_id)

    read_only_preflight = lambda payload: {"risk_level": "READ_ONLY", "resource_key": f"runtime:{payload.get('service', '')}", "host": "local"}

    registry.register(ToolDefinition(
        "runtime_status",
        "Consulta estado estruturado de serviços persistentes registrados (state, PID, health, readiness, uptime, restarts). Fonte preferida sobre Get-Process/tasklist para serviços cadastrados; sem argumento lista todos.",
        RiskLevel.READ_ONLY, RuntimeStatusInput,
        lambda service="", approval_id=None, reason="": runtime_one(service) if service else runtime_all(),
        dynamic_risk=False, llm_enabled=True, preflight=read_only_preflight,
    ))
    registry.register(ToolDefinition(
        "runtime_health",
        "Executa health check real agora (HTTP/TCP/PROCESS/COMMAND/Warm Manager) do serviço registrado e reporta healthy/latência/detalhe.",
        RiskLevel.READ_ONLY, RuntimeServiceInput,
        lambda service, approval_id=None, reason="": runtime_health(service),
        dynamic_risk=False, llm_enabled=True, preflight=read_only_preflight,
    ))
    registry.register(ToolDefinition(
        "runtime_logs",
        "Lê as últimas linhas do log do serviço registrado com redaction e truncamento (padrão 100 linhas).",
        RiskLevel.READ_ONLY, RuntimeLogsInput,
        lambda service, lines=100, approval_id=None, reason="": runtime_logs(service, lines),
        dynamic_risk=False, llm_enabled=True, preflight=read_only_preflight,
    ))
    registry.register(ToolDefinition(
        "runtime_start",
        "Inicia serviço registrado de forma idempotente e aguarda health/readiness reais antes de retornar.",
        RiskLevel.LOW_RISK, RuntimeServiceInput, mutate_start,
        dynamic_risk=True, llm_enabled=True, preflight=_mutation_preflight(supervisor, "start"),
    ))
    registry.register(ToolDefinition(
        "runtime_stop",
        "Para serviço registrado com encerramento gracioso seguido de confirmação de ausência. Exige approval vinculada.",
        RiskLevel.ELEVATED, RuntimeServiceInput, mutate_stop,
        dynamic_risk=True, llm_enabled=True, preflight=_mutation_preflight(supervisor, "stop"),
    ))
    registry.register(ToolDefinition(
        "runtime_restart",
        "Reinicia serviço registrado seguindo STATUS→STOP→VERIFY→START→HEALTH→READY; sucesso só com verificação real. Exige approval vinculada.",
        RiskLevel.ELEVATED, RuntimeServiceInput, mutate_restart,
        dynamic_risk=True, llm_enabled=True, preflight=_mutation_preflight(supervisor, "restart"),
    ))
