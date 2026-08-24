from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Type

from pydantic import BaseModel

from app.tools.models import EmptyInput, HostInput, HttpInput, NetworkHistoryInput, NetworkWindowInput, PortInput, RiskLevel, ToolResult
from app.tools.network import (
    check_http_service,
    dns_lookup,
    get_local_system_stats,
    get_network_interfaces,
    ping_host,
    tcp_port_check,
)
from app.tools.shell_models import ShellExecuteInput
from app.tools.remote_models import RemoteShellExecuteInput


logger = logging.getLogger("nyra.tools")
ToolCallable = Callable[..., Awaitable[dict[str, Any]]]
PreflightCallable = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    risk: RiskLevel
    input_model: Type[BaseModel]
    function: ToolCallable
    dynamic_risk: bool = False
    llm_enabled: bool = True
    preflight: PreflightCallable | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._remote_shell_service = None

    def register(self, definition: ToolDefinition) -> None:
        self._tools[definition.name] = definition

    def descriptions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "risk": "DYNAMIC" if tool.dynamic_risk else tool.risk.value,
                "enabled_for_llm": tool.llm_enabled,
                "input_schema": tool.input_model.model_json_schema(),
            }
            for tool in self._tools.values()
        ]

    def llm_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_model.model_json_schema(),
                },
            }
            for tool in self._tools.values()
            if tool.llm_enabled
        ]

    def should_route_to_agent(self, text: str) -> bool:
        """Use tool schemas only for requests that require observable state or action."""
        value = " ".join(text.casefold().split())
        homelab_target = re.search(
            r"\b(homelab|proxmox|openwrt|pve|home\s*assistant|homeassistant|\bha\b|hipervisor|hypervisor|"
            r"dc1|m[aá]quinas?\s+virtuais?|vms?|containers?|lxc)\b",
            value,
        )
        if homelab_target and re.search(
            r"\b(verifica|verificar|confere|conferir|checa|checar|diagnostica|status|estado|est[aã]o?"
            r"|online|offline|ligad[oa]s?|desligad[oa]s?|rodando|rodando\?|ativas?|ativos?|parad[oa]s?|"
            r"reinicia|reinicie|inicia|inicie|desliga|desligue|para a?|listar?|liste|mostra|mostre|"
            r"quais|quantas?|quantos?|storage|entidades?|vers?[aã]o|tarefas?)\b",
            value,
        ):
            return True
        if self.resolve_remote_target(value) is not None and re.search(
            r"\b(verifica|verificar|confere|checa|diagnostica|online|saud[aá]vel|responde|status|logs?|vms?|storage|wan|wi-?fi)\b",
            value,
        ):
            return True
        intent = re.search(
            r"\b(pinga|ping|executa|rode|roda|verifica|verificar|confere|checa|diagnostica|descobre|"
            r"mostra|liste|lista|reinicia|reinicie|inicia|inicie|abre|abra|abrir|feche|fecha|fechar|encerre|encerar|"
            r"escreve|escreva|escrever|digita|digite|digitar|clica|clique|clicar|salva|salve|salvar|"
            r"minimize|minimiza|minimizar|maximize|maximiza|restaura|restaurar|traz|traga|trazer|foca|focar|foco|"
            r"move|mova|redimensiona|navega|navegue|acessa|acesse|abre a pasta|instala|instale|desinstala|"
            r"para|cancela|existe|qual processo|quem usa|est[aá] abert[oa]|est[aá] fechad[oa]|est[aá] rodando|est[aá] saud[aá]vel|saud[aá]vel\?|"
            r"est[aá] online|est[aá] respondendo|est[aá] pronto|olha os logs|logs? do|status do|"
            r"sobe|suba|derruba|pid|hasexited|start-process)\b",
            value,
        )
        target = re.search(
            r"\b(rede|gateway|roteador|openwrt|proxmox|servidor|porta|processo|servi[cç]os?|backend|"
            r"docker|git|ollama|powershell|cmd|arquivo|diret[oó]rio|pasta|interface|dns|ip|cpu|mem[oó]ria|disco|"
            r"aplicativo|programa|execut[aá]vel|notepad|bloco\s+de\s+notas|calculadora|janela[s]?|runtime|"
            r"vs\s?code|visual studio code|chrome|edge|firefox|explorer|discord|spotify|obs|terminal|"
            r"[aá]rea de trabalho|desktop|tela)\b",
            value,
        )
        return bool(intent and target)

    def preflight(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        definition = self._tools.get(name)
        if not definition:
            return {"risk_level": RiskLevel.ELEVATED.value, "resource_key": f"tool:{name}"}
        try:
            validated = definition.input_model.model_validate(payload).model_dump()
        except Exception:
            return {"risk_level": definition.risk.value, "resource_key": f"tool:{name}"}
        if definition.preflight:
            return definition.preflight(validated)
        return {"risk_level": definition.risk.value, "resource_key": f"tool:{name}"}

    def resolve_remote_target(self, text: str) -> dict[str, str] | None:
        if self._remote_shell_service is None:
            return None
        host = self._remote_shell_service.hosts.find_remote_in_text(text)
        if host is None:
            return None
        return {"host": host.id, "address": host.address}

    async def execute(self, name: str, payload: dict[str, Any]) -> ToolResult:
        definition = self._tools.get(name)
        if not definition:
            raise KeyError(f"Ferramenta não permitida: {name}")
        validated = definition.input_model.model_validate(payload)
        started = time.perf_counter()
        ok = True
        try:
            data = await definition.function(**validated.model_dump())
            if "success" in data:
                ok = bool(data["success"])
        except Exception as exc:
            ok = False
            data = {"error": type(exc).__name__, "message": str(exc)[:300]}
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        risk_value = data.get("risk_level", definition.risk.value)
        try:
            actual_risk = RiskLevel(risk_value)
        except ValueError:
            actual_risk = definition.risk
        logger.info(
            "tool_executed",
            extra={"tool": name, "risk": actual_risk.value, "ok": ok, "elapsed_ms": elapsed_ms},
        )
        return ToolResult(
            tool=name,
            risk=actual_risk,
            ok=ok,
            data=data,
            elapsed_ms=elapsed_ms,
        )


def create_tool_registry(shell_service=None, remote_shell_service=None) -> ToolRegistry:
    registry = ToolRegistry()
    # These structured probes remain available to API, monitors and skills. Ad-hoc
    # conversational diagnostics intentionally go through the auditable system_shell.
    registry.register(ToolDefinition("ping_host", "Verifica disponibilidade ICMP.", RiskLevel.READ_ONLY, HostInput, ping_host, llm_enabled=False))
    registry.register(ToolDefinition("dns_lookup", "Resolve DNS de um host.", RiskLevel.READ_ONLY, HostInput, dns_lookup, llm_enabled=False))
    registry.register(ToolDefinition("tcp_port_check", "Testa conexão TCP em porta validada.", RiskLevel.READ_ONLY, PortInput, tcp_port_check, llm_enabled=False))
    registry.register(ToolDefinition("get_local_system_stats", "Lê CPU, memória, disco e uptime locais.", RiskLevel.READ_ONLY, EmptyInput, get_local_system_stats, llm_enabled=False))
    registry.register(ToolDefinition("get_network_interfaces", "Lista interfaces e endereços locais.", RiskLevel.READ_ONLY, EmptyInput, get_network_interfaces, llm_enabled=False))
    registry.register(ToolDefinition("check_http_service", "Consulta saúde básica de HTTP/HTTPS.", RiskLevel.READ_ONLY, HttpInput, check_http_service, llm_enabled=False))
    if shell_service is not None:
        registry.register(
            ToolDefinition(
                "system_shell",
                "Executa um comando local real em PowerShell ou CMD. Use para observar o sistema, rede, arquivos, processos, serviços, Git, Docker, Ollama e ambiente de desenvolvimento; resultados são classificados, auditados, limitados e ações sensíveis exigem approval_id vinculado.",
                RiskLevel.READ_ONLY,
                ShellExecuteInput,
                shell_service.execute,
                dynamic_risk=True,
                llm_enabled=bool(shell_service.settings.shell_enabled),
                preflight=shell_service.preflight,
            )
        )
    if remote_shell_service is not None:
        registry._remote_shell_service = remote_shell_service
        registry.register(
            ToolDefinition(
                "remote_shell",
                "Executa comando SSH real somente em host lógico cadastrado. Nunca informe IP, usuário, porta ou credenciais. Prefira diagnóstico read-only; alterações remotas passam por capability, risco, approval e verificação.",
                RiskLevel.READ_ONLY,
                RemoteShellExecuteInput,
                remote_shell_service.execute,
                dynamic_risk=True,
                llm_enabled=bool(remote_shell_service.settings.remote_shell_enabled),
                preflight=remote_shell_service.preflight,
            )
        )
    return registry


def register_network_watch_tools(registry: ToolRegistry, monitor) -> None:
    async def get_network_status() -> dict[str, Any]:
        return monitor.status()

    async def get_network_metrics(minutes: int = 5) -> dict[str, Any]:
        return {"minutes": minutes, "samples": monitor.sample_window(minutes)}

    async def get_recent_network_events(hours: int = 24, limit: int = 50) -> dict[str, Any]:
        return {"events": await monitor.history.recent(hours, limit)}

    async def get_network_quality_summary(hours: int = 1, limit: int = 50) -> dict[str, Any]:
        return {**await monitor.history.summary(hours), "current": monitor.status()}

    registry.register(ToolDefinition("get_network_status", "Lê o status atual do Network Watch.", RiskLevel.READ_ONLY, EmptyInput, get_network_status))
    registry.register(ToolDefinition("get_network_metrics", "Lê métricas recentes de latência, jitter, perda e throughput.", RiskLevel.READ_ONLY, NetworkWindowInput, get_network_metrics))
    registry.register(ToolDefinition("get_recent_network_events", "Lista eventos recentes de conectividade.", RiskLevel.READ_ONLY, NetworkHistoryInput, get_recent_network_events))
    registry.register(ToolDefinition("get_network_quality_summary", "Resume a qualidade de rede em uma janela de horas.", RiskLevel.READ_ONLY, NetworkHistoryInput, get_network_quality_summary))
