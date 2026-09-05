from __future__ import annotations

import functools
import inspect
import json
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
from app.core.turn import get_current_turn_id


logger = logging.getLogger("kazumi.tools")
ToolCallable = Callable[..., Awaitable[dict[str, Any]]]
PreflightCallable = Callable[[dict[str, Any]], dict[str, Any]]


@functools.lru_cache(maxsize=512)
def _cached_json_schema(model: Type[BaseModel]) -> str:
    """Pydantic schema JSON cached per input-model class (schema cost budget)."""
    return json.dumps(model.model_json_schema(), ensure_ascii=False)


# Domínios de roteamento (Apêndice PRO A). Um subconjunto pequeno de tools é
# entregue ao LLM por turno; CONVERSATION não recebe nenhum schema.
DOMAIN_CONVERSATION = "CONVERSATION"
DOMAIN_DESKTOP = "DESKTOP"
DOMAIN_FILESYSTEM = "FILESYSTEM"
DOMAIN_RUNTIME = "RUNTIME"
DOMAIN_HOMELAB_PROXMOX = "HOMELAB.PROXMOX"
DOMAIN_HOMELAB_HA = "HOMELAB.HOME_ASSISTANT"
DOMAIN_HOMELAB_OPENWRT = "HOMELAB.OPENWRT"
DOMAIN_NETWORK = "NETWORK"
DOMAIN_BROWSER = "BROWSER"
DOMAIN_WEB_RESEARCH = "WEB_RESEARCH"
DOMAIN_WORKFLOW = "WORKFLOW"
DOMAIN_GENERIC = "GENERIC"

_DOMAIN_TOOL_PREFIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("web_",), DOMAIN_WEB_RESEARCH),
    (("desktop_", "ui_", "clipboard_"), DOMAIN_DESKTOP),
    (("browser_",), DOMAIN_BROWSER),
    (("proxmox", "pve"), DOMAIN_HOMELAB_PROXMOX),
    (("home_assistant", "ha_"), DOMAIN_HOMELAB_HA),
    (("openwrt",), DOMAIN_HOMELAB_OPENWRT),
    (("get_network_", "network_"), DOMAIN_NETWORK),
    (("filesystem_", "file_"), DOMAIN_FILESYSTEM),
    (("process_", "windows_service_", "registry_", "task_", "system_shell"), DOMAIN_RUNTIME),
    (("remote_shell",), DOMAIN_GENERIC),
    (("workflow", "skill_"), DOMAIN_WORKFLOW),
)


def classify_domain(text: str) -> str:
    """Classify the operator request into one routing domain before tool selection."""
    from app.usb.hardware import hardware_request
    from app.web_research.planner import standalone_research_request
    if standalone_research_request(text):
        return DOMAIN_WEB_RESEARCH
    if hardware_request(text) is not None:
        return DOMAIN_GENERIC
    value = " ".join(text.casefold().split())
    if re.search(r"\b(clipboard|transfer\w*)\b", value):
        return DOMAIN_DESKTOP
    if re.search(
        r"\b(proxmox|pve|\bvm[s]?\b|m[aá]quinas?\s+virtuais?|hipervisor|hypervisor|lxc|container[s]?\s+do\s+(?:proxmox|servidor))\b",
        value,
    ):
        return DOMAIN_HOMELAB_PROXMOX
    if re.search(r"\b(home\s*assistant|homeassistant|\bha\b|automa[cç][aã]o\s+da?\s+casa|entidades?|luzes?|interruptores?)\b", value):
        return DOMAIN_HOMELAB_HA
    if re.search(r"\b(openwrt|roteador|gateway|router)\b", value):
        return DOMAIN_HOMELAB_OPENWRT
    if re.search(
        r"\b(abre|abra|abrir|feche|fecha|fechar|minimiza|minimize|restaura|restaurar|maximiza|maximize|foca|focar|"
        r"bloco\s+de\s+notas|notepad|calculadora|calculator|explorador|explorer|vs\s?code|visual\s+studio\s+code|"
        r"janela[s]?|[aá]rea\s+de\s+trabalho)\b",
        value,
    ):
        return DOMAIN_DESKTOP
    if re.search(r"\b(navegador|chrome|edge|firefox|aba[s]?|site|url|pesquis[ae]\s+na\s+internet)\b", value):
        return DOMAIN_BROWSER
    if re.search(r"\b(arquivo[s]?|pasta[s]?|diret[oó]rio[s]?|lista\s+arquivos)\b", value):
        return DOMAIN_FILESYSTEM
    if re.search(r"\b(rede|internet|conex[aã]o|lat[eê]ncia|jitter|dns|ping|wi-?fi|interface[s]?|porta[s]?)\b", value):
        return DOMAIN_NETWORK
    if re.search(
        r"\b(processo[s]?|servi[cç]o[s]?|registro|tarefa[s]?|shell|powershell|cmd|docker|git|ollama|backend|runtime)\b",
        value,
    ):
        return DOMAIN_RUNTIME
    if re.search(r"\b(workflow|fluxo|rotina|automa[cç][aã]o)\b", value):
        return DOMAIN_WORKFLOW
    if re.search(
        r"\b(monitora|monitore|monitorar|acompanha|acompanhe|acompanhar|"
        r"avisa|avise|avisar)\b|\bquando\b.{0,80}\b(?:mudar|ficar|chegar|terminar)\b",
        value,
    ):
        return DOMAIN_GENERIC
    return DOMAIN_CONVERSATION


def _tool_domain(name: str) -> str:
    lowered = name.casefold()
    for prefixes, domain in _DOMAIN_TOOL_PREFIXES:
        if lowered.startswith(prefixes) or lowered in prefixes:
            return domain
    return DOMAIN_GENERIC


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
        self._result_observers: list[Callable[..., Any]] = []

    def register(self, definition: ToolDefinition) -> None:
        self._tools[definition.name] = definition

    def add_result_observer(self, observer: Callable[..., Any]) -> None:
        """Observa metadados de resultados sem alterar execução ou grounding."""
        if observer not in self._result_observers:
            self._result_observers.append(observer)

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

    def llm_tools(self, domain: str | None = None) -> list[dict[str, Any]]:
        """OpenAI-style schemas, optionally narrowed to one routing domain.

        DOMAIN_CONVERSATION yields no tools; DOMAIN_GENERIC keeps every
        LLM-enabled tool as safe fallback when routing is ambiguous.
        """
        tools = [tool for tool in self._tools.values() if tool.llm_enabled]
        if domain is not None and domain != DOMAIN_GENERIC:
            if domain == DOMAIN_CONVERSATION:
                tools = []
            else:
                primary = [tool for tool in tools if _tool_domain(tool.name) == domain]
                support_names = (
                    {"remote_shell", "desktop_windows", "desktop_find_application"}
                    if domain == DOMAIN_DESKTOP
                    else {"system_shell", "remote_shell", "desktop_windows", "desktop_find_application"}
                )
                if domain == DOMAIN_WEB_RESEARCH:
                    support_names = set()
                else:
                    support_names |= {"monitor_create", "monitor_status", "monitor_list", "monitor_cancel"}
                support = [
                    tool for tool in tools
                    if tool.name in support_names and _tool_domain(tool.name) != domain
                ]
                tools = primary + support
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": json.loads(_cached_json_schema(tool.input_model)),
                },
            }
            for tool in tools
        ]

    def should_route_to_agent(self, text: str) -> bool:
        """Use tool schemas only for requests that require observable state or action."""
        from app.usb.hardware import hardware_request
        from app.web_research.planner import standalone_research_request
        if standalone_research_request(text):
            return True
        if hardware_request(text) is not None:
            return True
        value = " ".join(text.casefold().split())
        if re.search(
            r"\b(monitora|monitore|monitorar|monitoramento|acompanha|acompanhe|acompanhar|"
            r"avisa|avise|avisar|fica\s+de\s+olho)\b|"
            r"\bquando\b.{0,100}\b(?:mudar|ficar|chegar|cair|subir|terminar|concluir)\b",
            value,
        ):
            return True
        if re.search(r"\b(clipboard|transfer\w*)\b", value) and re.search(
            r"\b(status|estado|copia|copie|copiar|cola|cole|colar|limpa|limpar|texto)\b",
            value,
        ):
            return True
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
        if intent and target:
            return True
        # Comando imperativo no início da frase é ação mesmo sem palavra-alvo
        # conhecida (ex.: "rode os testes", "abre o zumbi runner") — o domínio
        # ambíguo cai no subset GENERIC em vez de conversa sem tools.
        imperative = re.match(
            r"^\s*(?:kazumi[, ]+)?(?:abre|abra|abrir|feche|fecha|fechar|executa|execute|rode|roda|rodar|"
            r"inicia|inicie|minimize|minimiza|restaura|maximize|foca|liste|lista|verifica|reinicia|reinicie)\b",
            value,
        )
        return bool(imperative and len(value.split()) <= 12)

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

    def is_exposed(self, name: str) -> bool:
        """Return whether a tool may be reached from LLM/API composition."""
        definition = self._tools.get(name)
        return bool(definition and definition.llm_enabled)

    async def execute(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        exposure: str = "internal",
    ) -> ToolResult:
        definition = self._tools.get(name)
        if not definition:
            raise KeyError(f"Ferramenta não permitida: {name}")
        if exposure in {"llm", "api"} and not definition.llm_enabled:
            raise PermissionError(
                f"Ferramenta '{name}' não está autorizada para execução via {exposure}."
            )
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
        result = ToolResult(
            tool=name,
            risk=actual_risk,
            ok=ok,
            data=data,
            elapsed_ms=elapsed_ms,
        )
        for observer in tuple(self._result_observers):
            try:
                observed = observer(
                    name, dict(payload), result, get_current_turn_id(),
                )
                if inspect.isawaitable(observed):
                    await observed
            except Exception as error:  # noqa: BLE001
                logger.warning(
                    "tool_result_observer_failed tool=%s type=%s",
                    name, type(error).__name__,
                )
        return result


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
