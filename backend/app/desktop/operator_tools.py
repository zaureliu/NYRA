"""Local Operator tools: filesystem / process / service / registry / task.

Read operations are READ_ONLY; mutations carry policy risk and reuse the shared
single-use ShellApprovalGate (APPROVAL_REQUIRED → WAITING_APPROVAL → consume
approval_id), exactly like system_shell/remote_shell. Service and registry
mutations additionally flow through the legitimate UAC Elevated Broker.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.desktop.operator import OperatorController
from app.tools.models import RiskLevel
from app.tools.registry import ToolDefinition


class FsPathInput(BaseModel):
    path: str = Field(min_length=2, max_length=500)
    # approval_id precisa existir no schema: sem ele o registry descarta a
    # chave na validação e o fluxo two-phase de approval nunca consome.
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)


class FsListInput(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=100, ge=1, le=400)


class FsReadInput(BaseModel):
    path: str = Field(min_length=2, max_length=500)


class FsWriteInput(BaseModel):
    path: str = Field(min_length=2, max_length=500)
    content: str = Field(default="", max_length=200_000)
    append: bool = False
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)


class FsTransferInput(BaseModel):
    source: str = Field(min_length=2, max_length=500)
    destination: str = Field(min_length=2, max_length=500)
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)


class FsRenameInput(FsPathInput):
    new_name: str = Field(min_length=1, max_length=200)
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)


class FsDeleteInput(FsPathInput):
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)
    reason: str = Field(default="", max_length=300)


class FsSearchInput(BaseModel):
    root: str = Field(min_length=2, max_length=500)
    pattern: str = Field(min_length=2, max_length=120)
    limit: int = Field(default=50, ge=1, le=100)


class ProcessListInput(BaseModel):
    sort_by: str = Field(default="memory", max_length=20, pattern=r"^[a-z]*$")
    limit: int = Field(default=25, ge=1, le=80)


class ProcessStatusInput(BaseModel):
    pid: int | None = Field(default=None, ge=1)
    name: str = Field(default="", max_length=120)


class ProcessStartInput(BaseModel):
    executable: str = Field(min_length=2, max_length=400)
    arguments: str = Field(default="", max_length=1000)


class ProcessStopInput(BaseModel):
    pid: int = Field(ge=1)
    force: bool = False
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)
    reason: str = Field(default="", max_length=300)


class ServiceListInput(BaseModel):
    status: str = Field(default="", max_length=20, pattern=r"^[a-z]*$")
    limit: int = Field(default=40, ge=1, le=120)


class ServiceActionInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)
    reason: str = Field(default="", max_length=300)


class RegistryReadInput(BaseModel):
    key_path: str = Field(min_length=5, max_length=300)
    value_name: str = Field(default="", max_length=64)


class RegistrySetInput(BaseModel):
    key_path: str = Field(min_length=5, max_length=300)
    value_name: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=0, max_length=2000)
    reg_type: str = Field(default="REG_SZ", max_length=20, pattern=r"^REG_[A-Z]+$")
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)
    reason: str = Field(default="", max_length=300)


class TaskListInput(BaseModel):
    folder: str = Field(default="\\", max_length=120)


class TaskActionInput(BaseModel):
    name: str = Field(min_length=2, max_length=180)
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)
    reason: str = Field(default="", max_length=300)


def _preflight(resource_prefix: str, risk: str):
    return lambda payload: {
        "risk_level": risk,
        "resource_key": f"{resource_prefix}:{str(payload.get('path') or payload.get('pid') or payload.get('name') or payload.get('key_path') or payload.get('task') or '')[:60]}",
        "host": "local",
    }


def register_operator_tools(registry, controller: OperatorController) -> None:
    # ---------------- filesystem
    registry.register(ToolDefinition(
        "filesystem_list",
        "Lista arquivos e pastas de um diretório local com tipo e tamanho (somente leitura).",
        RiskLevel.READ_ONLY, FsListInput,
        lambda path, limit=100, **_: controller.fs_list(path, int(limit)),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("fs", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "filesystem_read",
        "Lê um arquivo de texto local (até ~256KB) com encoding detectado.",
        RiskLevel.READ_ONLY, FsReadInput,
        lambda path, **_: controller.fs_read(path),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("fs", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "filesystem_write",
        "Cria/sobrescreve (ou anexa) um arquivo local e VERIFICA existência+tamanho depois. Exige approval_id.",
        RiskLevel.LOW_RISK, FsWriteInput,
        lambda **kwargs: controller.fs_write(
            kwargs.get("path", ""), kwargs.get("content", ""),
            append=bool(kwargs.get("append")), approval_id=kwargs.get("approval_id"),
        ),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("fs", "LOW_RISK"),
    ))
    registry.register(ToolDefinition(
        "filesystem_copy",
        "Copia arquivo/pasta e verifica o destino. Exige approval_id.",
        RiskLevel.LOW_RISK, FsTransferInput,
        lambda source, destination, approval_id=None, **_: controller.fs_copy(source, destination, approval_id),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("fs", "LOW_RISK"),
    ))
    registry.register(ToolDefinition(
        "filesystem_move",
        "Move arquivo/pasta e verifica origem+destino. Exige approval_id.",
        RiskLevel.LOW_RISK, FsTransferInput,
        lambda source, destination, approval_id=None, **_: controller.fs_move(source, destination, approval_id),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("fs", "LOW_RISK"),
    ))
    registry.register(ToolDefinition(
        "filesystem_rename",
        "Renomeia arquivo/pasta no mesmo diretório e verifica. Exige approval_id.",
        RiskLevel.LOW_RISK, FsRenameInput,
        lambda path, new_name, approval_id=None, **_: controller.fs_rename(path, new_name, approval_id),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("fs", "LOW_RISK"),
    ))
    registry.register(ToolDefinition(
        "filesystem_delete",
        "EXCLUI arquivo ou pasta (DESTRUCTIVE): exige approval_id vinculado e bloqueia caminhos protegidos (raiz/home/projeto). Nunca execute sem pedido explícito do operador.",
        RiskLevel.DESTRUCTIVE, FsDeleteInput,
        lambda path, approval_id=None, reason="", **_: controller.fs_delete(path, approval_id, reason),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("fs", "DESTRUCTIVE"),
    ))
    registry.register(ToolDefinition(
        "filesystem_mkdir",
        "Cria diretório (com pais) e verifica. Exige approval_id.",
        RiskLevel.LOW_RISK, FsPathInput,
        lambda path, approval_id=None, **_: controller.fs_mkdir(path, approval_id),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("fs", "LOW_RISK"),
    ))
    registry.register(ToolDefinition(
        "filesystem_search",
        "Busca arquivos/pastas por padrão textual a partir de uma raiz (limitado).",
        RiskLevel.READ_ONLY, FsSearchInput,
        lambda root, pattern, limit=50, **_: controller.fs_search(root, pattern, int(limit)),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("fs", "READ_ONLY"),
    ))

    # ---------------- processes
    registry.register(ToolDefinition(
        "process_list",
        "Lista processos locais com memória (ordenável) e marca componentes protegidos da NYRA.",
        RiskLevel.READ_ONLY, ProcessListInput,
        lambda sort_by="memory", limit=25, **_: controller.process_list(sort_by, int(limit)),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("proc", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "process_status",
        "Status detalhado de um processo por PID ou nome (exe, cmdline fingerprint, create_time).",
        RiskLevel.READ_ONLY, ProcessStatusInput,
        lambda pid=None, name="", **_: controller.process_status(pid, name or ""),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("proc", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "process_start",
        "Compatibilidade de API: inicialização arbitrária está bloqueada; use system_shell.",
        RiskLevel.ELEVATED, ProcessStartInput,
        lambda executable, arguments="", **_: controller.process_start(executable, arguments),
        dynamic_risk=False, llm_enabled=False, preflight=_preflight("proc", "ELEVATED"),
    ))
    registry.register(ToolDefinition(
        "process_stop",
        "Para um processo por PID (terminate→kill como fallback interno; componentes NYRA são bloqueados). Exige approval_id.",
        RiskLevel.ELEVATED, ProcessStopInput,
        lambda pid, force=False, approval_id=None, reason="", **_: controller.process_stop(int(pid), approval_id, bool(force), reason),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("proc", "ELEVATED"),
    ))

    # ---------------- services
    registry.register(ToolDefinition(
        "windows_service_list",
        "Lista serviços do Windows com estado (running/stopped/paused); filtre por status.",
        RiskLevel.READ_ONLY, ServiceListInput,
        lambda status="", limit=40, **_: controller.service_list(status, int(limit)),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("svc", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "windows_service_status",
        "Estado atual de um serviço específico pelo nome.",
        RiskLevel.READ_ONLY, ServiceActionInput,
        lambda name, **_: _service_status(controller, name),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("svc", "READ_ONLY"),
    ))
    for action in ("start", "stop", "restart"):
        registry.register(ToolDefinition(
            f"windows_service_{action}",
            f"{action.capitalize()} de serviço Windows via PowerShell elevado legítimo (UAC) após approval_id do operador; verifica o Status depois ({'RUNNING/STOPPED'}).",
            RiskLevel.ELEVATED, ServiceActionInput,
            lambda name, approval_id=None, reason="", __action=action, **_kwargs: controller.service_action(
                __action, name, approval_id, reason,
            ),
            dynamic_risk=False, llm_enabled=True, preflight=_preflight("svc", "ELEVATED"),
        ))

    # ---------------- registry
    registry.register(ToolDefinition(
        "registry_read",
        "Lê chave/valor do Registro do Windows via reg query (HKLM/HKCU/HKCR/HKU/HKCC). Somente leitura.",
        RiskLevel.READ_ONLY, RegistryReadInput,
        lambda key_path, value_name="", **_: controller.registry_read(key_path, value_name),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("reg", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "registry_set",
        "Grava valor no Registro via elevação legítima (UAC) após approval_id; guarda backup textual do valor anterior e RELÊ para confirmar. Tipos: REG_SZ/REG_DWORD/REG_QWORD/REG_BINARY/REG_EXPAND_SZ.",
        RiskLevel.ELEVATED, RegistrySetInput,
        lambda key_path, value_name, value, reg_type="REG_SZ", approval_id=None, reason="", **_kwargs: controller.registry_set(
            key_path, value_name, value, reg_type, approval_id, reason,
        ),
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("reg", "ELEVATED"),
    ))

    # ---------------- scheduled tasks
    async def _task_list(folder="\\", **_):
        return await controller.task_list(folder)

    async def _task_run(name, approval_id=None, reason="", **_):
        return await controller.task_action("run", name, approval_id, reason)

    async def _task_delete(name, approval_id=None, reason="", **_):
        return await controller.task_action("delete", name, approval_id, reason)

    registry.register(ToolDefinition(
        "task_list",
        "Lista tarefas agendadas do Windows (schtasks query, somente leitura).",
        RiskLevel.READ_ONLY, TaskListInput, _task_list,
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("task", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "task_run",
        "Executa uma tarefa agendada EXISTENTE pelo nome; exige approval_id e confirma pela releitura do agendador.",
        RiskLevel.ELEVATED, TaskActionInput, _task_run,
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("task", "ELEVATED"),
    ))
    registry.register(ToolDefinition(
        "task_delete",
        "Remove uma tarefa agendada (DESTRUCTIVE); exige approval_id e confirma ausência na releitura.",
        RiskLevel.DESTRUCTIVE, TaskActionInput, _task_delete,
        dynamic_risk=False, llm_enabled=True, preflight=_preflight("task", "DESTRUCTIVE"),
    ))


class BrowserUrlInput(BaseModel):
    url: str = Field(min_length=6, max_length=2000)
    browser: str = Field(default="", max_length=20, pattern=r"^[a-z]*$")


class BrowserNavigateInput(BrowserUrlInput):
    tab_id: str = Field(default="", max_length=64)


class BrowserTabInput(BaseModel):
    tab_id: str = Field(min_length=2, max_length=64)


class BrowserTabsInput(BaseModel):
    placeholder: str = Field(default="", max_length=4)


def register_browser_tools(registry, browser_controller) -> None:
    async def browser_open(url, browser="", **_):
        return await browser_controller.open(url, browser)

    async def browser_navigate(url, tab_id="", **_):
        return await browser_controller.navigate(url, tab_id)

    async def browser_tabs(**_):
        return await browser_controller.tabs()

    async def browser_close_tab(tab_id, **_):
        return await browser_controller.close_tab(tab_id)

    read_preflight = lambda payload: {"risk_level": "READ_ONLY", "resource_key": "browser:tabs", "host": "local"}
    action_preflight = lambda action: lambda payload: {
        "risk_level": "LOW_RISK",
        "resource_key": f"browser:{action}:{str(payload.get('url') or payload.get('tab_id') or '')[:50]}",
        "host": "local",
    }

    registry.register(ToolDefinition(
        "browser_open",
        "Abre o navegador gerenciado (Chrome/Edge com perfil próprio da NYRA e CDP ativo) numa URL http(s); confirma pela lista de abas.",
        RiskLevel.LOW_RISK, BrowserUrlInput, browser_open,
        dynamic_risk=False, llm_enabled=True, preflight=action_preflight("open"),
    ))
    registry.register(ToolDefinition(
        "browser_navigate",
        "Navega uma aba do navegador gerenciado para outra URL http(s) e VERIFICA a URL atual via CDP.",
        RiskLevel.LOW_RISK, BrowserNavigateInput, browser_navigate,
        dynamic_risk=False, llm_enabled=True, preflight=action_preflight("navigate"),
    ))
    registry.register(ToolDefinition(
        "browser_tabs",
        "Lista abas abertas do navegador gerenciado com título e URL (somente leitura; nunca expõe cookies/tokens).",
        RiskLevel.READ_ONLY, BrowserTabsInput, browser_tabs,
        dynamic_risk=False, llm_enabled=True, preflight=read_preflight,
    ))
    registry.register(ToolDefinition(
        "browser_close_tab",
        "Fecha uma aba específica por id e confirma que ela sumiu da lista CDP.",
        RiskLevel.LOW_RISK, BrowserTabInput, browser_close_tab,
        dynamic_risk=False, llm_enabled=True, preflight=action_preflight("close_tab"),
    ))
    for action in ("refresh", "back", "forward"):
        registry.register(ToolDefinition(
            f"browser_{action}",
            f"{action.capitalize()} na aba ativa do navegador gerenciado via comando DevTools.",
            RiskLevel.LOW_RISK, BrowserTabsInput,
            lambda __action=action, **_kwargs: browser_controller.page_command(__action),
            dynamic_risk=False, llm_enabled=True, preflight=action_preflight(action),
        ))


class PowerActionInput(BaseModel):
    action: str = Field(min_length=3, max_length=20, pattern=r"^[a-z]+$")
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)
    reason: str = Field(default="", max_length=300)


def register_power_tools(registry, controller: OperatorController) -> None:
    async def system_power(action, approval_id=None, reason="", **_):
        return await controller.system_power(action, approval_id, reason)

    def power_preflight(payload: dict) -> dict:
        risk = {"lock": "LOW_RISK", "sleep": "ELEVATED", "logoff": "CRITICAL",
                "restart": "CRITICAL", "shutdown": "CRITICAL"}.get(str(payload.get("action")), "ELEVATED")
        return {"risk_level": risk, "resource_key": f"power:{payload.get('action')}", "host": "local"}

    registry.register(ToolDefinition(
        "system_power",
        "lock/sleep/logoff/restart/shutdown do computador. lock é LOW_RISK; demais exigem approval_id explícito vinculado (restart/shutdown dão 30s para cancelar via 'shutdown /a'). Nunca use sem pedido claro do operador.",
        RiskLevel.CRITICAL, PowerActionInput, system_power,
        dynamic_risk=True, llm_enabled=True, preflight=power_preflight,
    ))


async def _service_status(controller: OperatorController, name: str) -> dict:
    result = await controller.service_list(name)
    services = [item for item in result.get("services", []) if item["name"].casefold() == name.casefold()]
    if not services:
        return {"success": False, "error_code": "SERVICE_NOT_FOUND", "message": f"Serviço '{name}' não encontrado."}
    return {"success": True, "effect_verified": True, "verification_status": "VERIFIED", "service": services[0]}
