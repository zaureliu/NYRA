"""Tool registration for Operator V2 capabilities.

Every tool follows the house pattern: Pydantic input schema, explicit risk,
preflight resource keys, and results carrying success/error_code/effect_
verified so grounding keeps working. Credential secrets NEVER appear in any
LLM-facing tool (§86/§89): list/status are metadata-only; use/create/delete/
rotate stay internal or operator-direct API actions.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from app.tools.models import RiskLevel
from app.tools.registry import ToolDefinition


# ------------------------------------------------------------------ input models
class VisionCaptureInput(BaseModel):
    target: str = Field(default="window", pattern=r"^(window|monitor|region)$")
    hwnd: int | None = Field(default=None, ge=1)
    monitor_id: int = Field(default=1, ge=0, le=8)
    region: dict[str, int] | None = None


class VisionFrameInput(BaseModel):
    frame_id: str = Field(min_length=6, max_length=64)


class VisionClickInput(VisionFrameInput):
    element_id: str = Field(min_length=3, max_length=24)
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)


class VisionTypeInput(VisionFrameInput):
    element_id: str = Field(min_length=3, max_length=24)
    text: str = Field(min_length=1, max_length=4000)
    secret: bool = False
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)


class VisionReadInput(VisionFrameInput):
    use_ocr: bool = False


class VisionDiffInput(BaseModel):
    before_frame_id: str = Field(min_length=6, max_length=64)
    after_frame_id: str = Field(min_length=6, max_length=64)


class EmptyV2Input(BaseModel):
    placeholder: str = Field(default="", max_length=4)


class ClipboardWriteInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class AdapterActionInput(BaseModel):
    app_id: str = Field(min_length=2, max_length=40)
    action: str = Field(min_length=2, max_length=60)
    params: dict[str, Any] = Field(default_factory=dict)
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)


class AdapterStatusInput(BaseModel):
    app_id: str = Field(min_length=2, max_length=40)


class BrowserTabIdInput(BaseModel):
    tab_id: str = Field(min_length=2, max_length=64)


class BrowserDomInspectInput(BaseModel):
    tab_id: str = Field(default="", max_length=64)
    max_nodes: int = Field(default=120, ge=10, le=300)


class BrowserFindInput(BaseModel):
    role: str = Field(default="", max_length=40)
    label: str = Field(default="", max_length=120)
    text: str = Field(default="", max_length=200)
    selector: str = Field(default="", max_length=500)
    tab_id: str = Field(default="", max_length=64)
    limit: int = Field(default=10, ge=1, le=30)


class BrowserClickElementInput(BaseModel):
    selector: str = Field(default="", max_length=500)
    x: int = Field(default=0, ge=0, le=8000)
    y: int = Field(default=0, ge=0, le=8000)
    tab_id: str = Field(default="", max_length=64)
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)


class BrowserTypeInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    selector: str = Field(min_length=2, max_length=500)
    submit: bool = False
    secret: bool = False
    tab_id: str = Field(default="", max_length=64)
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)


class BrowserSelectOptionInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    selector: str = Field(min_length=2, max_length=500)
    tab_id: str = Field(default="", max_length=64)
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)


class BrowserSetCheckedInput(BaseModel):
    selector: str = Field(min_length=2, max_length=500)
    checked: bool = True
    tab_id: str = Field(default="", max_length=64)
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)


class BrowserWaitInput(BaseModel):
    condition: str = Field(pattern=r"^(navigation|element|network_idle|download)$")
    selector: str = Field(default="", max_length=500)
    timeout_seconds: float = Field(default=15.0, ge=2.0, le=60.0)
    tab_id: str = Field(default="", max_length=64)


class BrowserScriptInput(BaseModel):
    script: str = Field(min_length=4, max_length=8000)
    tab_id: str = Field(default="", max_length=64)
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)

class CredentialIdInput(BaseModel):
    credential_id: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9_]+$")


class ElevatedOpenInput(BaseModel):
    reason: str = Field(min_length=4, max_length=200)
    ttl_seconds: int = Field(default=300, ge=60, le=900)
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)


class ElevatedExecuteInput(BaseModel):
    session_id: str = Field(min_length=6, max_length=64)
    command: str = Field(min_length=2, max_length=4000)
    shell: str = Field(default="powershell", pattern=r"^(powershell|cmd)$")
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)


class JobStartInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    argv: list[str] = Field(min_length=1, max_length=40)
    working_directory: str = Field(default="", max_length=400)
    resource_key: str = Field(default="", max_length=120)


class JobIdInput(BaseModel):
    job_id: str = Field(min_length=6, max_length=64)


class TaskStepInput(BaseModel):
    step_id: str = Field(min_length=1, max_length=60)
    tool: str = Field(min_length=1, max_length=80)
    params: dict[str, Any] = Field(default_factory=dict)
    resource: str = Field(default="", max_length=120)
    depends_on: list[str] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)
    auto_rollback: bool = False


class TaskCreateInput(BaseModel):
    goal: str = Field(min_length=2, max_length=1000)
    steps: list[TaskStepInput] = Field(min_length=1, max_length=20)
    verification_plan: str = Field(default="", max_length=500)
    deadline_seconds: int | None = Field(default=None, ge=30, le=7200)


class TaskIdInput(BaseModel):
    task_id: str = Field(min_length=6, max_length=64)


class WatchRegisterInput(BaseModel):
    event_types: list[str] = Field(min_length=1, max_length=8)
    filters: dict[str, str] = Field(default_factory=dict)
    ttl_seconds: int = Field(default=300, ge=15, le=3600)


class WatchEventsInput(BaseModel):
    watch_id: str = Field(min_length=6, max_length=64)
    after_index: int = Field(default=0, ge=0)


class WorkflowCreateInput(BaseModel):
    workflow_id: str = Field(pattern=r"^wf_[a-z0-9_]{3,48}$")
    name: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=400)
    trigger_phrases: list[str] = Field(default_factory=list, max_length=10)
    steps: list[dict[str, Any]] = Field(min_length=1, max_length=20)
    parameters: dict[str, str] = Field(default_factory=dict)
    risk: str = Field(default="LOW_RISK",
                      pattern=r"^(READ_ONLY|LOW_RISK|ELEVATED|DESTRUCTIVE|CRITICAL)$")


class WorkflowRunInput(BaseModel):
    workflow_id: str = Field(min_length=6, max_length=64)
    parameters: dict[str, str] = Field(default_factory=dict)


def _local_preflight(prefix: str, risk: str):
    return lambda payload: {
        "risk_level": risk,
        "resource_key": f"{prefix}:{str(payload.get('frame_id') or payload.get('job_id') or payload.get('task_id') or payload.get('watch_id') or payload.get('workflow_id') or payload.get('credential_id') or payload.get('session_id') or payload.get('tab_id') or payload.get('app_id') or 'general')[:60]}",
        "host": "local",
    }


# --------------------------------------------------------------------- register
def register_operator_v2_tools(registry, service) -> None:
    """Register the full V2 tool surface onto the shared ToolRegistry."""

    _register_vision_tools(registry, service)
    _register_clipboard_tools(registry, service)
    _register_adapter_tools(registry, service)
    _register_browser_v2_tools(registry, service)
    _register_credential_tools(registry, service)
    _register_elevated_tools(registry, service)
    _register_job_tools(registry, service)
    _register_task_tools(registry, service)
    _register_watch_tools(registry, service)
    _register_workflow_tools(registry, service)


def _register_clipboard_tools(registry, service) -> None:
    clipboard = service.clipboard

    async def clipboard_status(**_):
        return await asyncio.to_thread(clipboard.status)

    async def clipboard_write_text(text, **_):
        return await asyncio.to_thread(clipboard.write_text, str(text))

    async def clipboard_clear(**_):
        return await asyncio.to_thread(clipboard.clear)

    registry.register(ToolDefinition(
        "clipboard_status",
        "Lê somente metadados do clipboard local (formatos/has_text/sequence); nunca retorna conteúdo.",
        RiskLevel.READ_ONLY, EmptyV2Input, clipboard_status,
        preflight=_local_preflight("clipboard", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "clipboard_write_text",
        "Substitui o clipboard local por texto explícito e verifica o formato Win32. A resposta nunca ecoa o conteúdo.",
        RiskLevel.LOW_RISK, ClipboardWriteInput, clipboard_write_text,
        preflight=_local_preflight("clipboard", "LOW_RISK"),
    ))
    registry.register(ToolDefinition(
        "clipboard_clear",
        "Limpa o clipboard local e verifica que nenhum formato permaneceu.",
        RiskLevel.LOW_RISK, EmptyV2Input, clipboard_clear,
        preflight=_local_preflight("clipboard", "LOW_RISK"),
    ))

def _register_vision_tools(registry, service) -> None:
    if service.vision is None:
        return
    vision = service.vision

    async def screen_capture(target="window", hwnd=None, monitor_id=1, region=None, **_):
        return await asyncio.to_thread(vision.capture, target=target, hwnd=hwnd,
                                       monitor_id=int(monitor_id), region=region)

    registry.register(ToolDefinition(
        "screen_capture",
        "Captura a tela de forma ESCOPADA (window/monitor/region) e devolve frame_id com TTL. Prefira window quando o alvo é conhecido; nunca envie imagem ao LLM (§11-§16).",
        RiskLevel.READ_ONLY, VisionCaptureInput, screen_capture,
        preflight=_local_preflight("vision", "READ_ONLY"),
    ))

    async def visual_inspect(frame_id, **_):
        return await vision.inspect(frame_id)

    registry.register(ToolDefinition(
        "visual_inspect",
        "Interpreta um frame: controles detectados via UIA (prioridade §19), regiões de texto, diálogos/modais e warnings. OCR só como fallback explícito via visual_read.",
        RiskLevel.READ_ONLY, VisionFrameInput, visual_inspect,
        preflight=_local_preflight("vision", "READ_ONLY"),
    ))

    async def visual_click(frame_id, element_id, approval_id=None, **_):
        return await vision.click(frame_id, element_id, approval_id=approval_id)

    registry.register(ToolDefinition(
        "visual_click",
        "Clica em elemento visual somente após approval one-use exato. Revalida integralmente o frame antes (FRAME_STALE) e verifica mudança depois.",
        RiskLevel.ELEVATED, VisionClickInput, visual_click,
        preflight=_local_preflight("vision", "ELEVATED"),
    ))

    async def visual_type(frame_id, element_id, text, secret=False, approval_id=None, **_):
        return await vision.type_text(
            frame_id, element_id, text, secret=bool(secret), approval_id=approval_id,
        )

    registry.register(ToolDefinition(
        "visual_type",
        "Digita em elemento visual somente após approval one-use exato (ValuePattern com read-back). Com secret=true o valor nunca é ecoado (§74).",
        RiskLevel.ELEVATED, VisionTypeInput, visual_type,
        preflight=_local_preflight("vision", "ELEVATED"),
    ))

    async def visual_read(frame_id, use_ocr=False, **_):
        return await vision.read(frame_id, use_ocr=bool(use_ocr))

    registry.register(ToolDefinition(
        "visual_read",
        "Extrai texto visível: UIA primeiro; OCR do Windows apenas se use_ocr=true e UIA vazio (§20/§26). Saída passa por redaction.",
        RiskLevel.READ_ONLY, VisionReadInput, visual_read,
        preflight=_local_preflight("vision", "READ_ONLY"),
    ))

    async def screen_diff(before_frame_id, after_frame_id, **_):
        return vision.compare(before_frame_id, after_frame_id)

    registry.register(ToolDefinition(
        "screen_diff",
        "Compara dois frames (antes/depois) para verificação visual de efeito (§27/§28).",
        RiskLevel.READ_ONLY, VisionDiffInput, screen_diff,
        preflight=_local_preflight("vision", "READ_ONLY"),
    ))

    async def detect_modals(**_):
        return await asyncio.to_thread(vision.detect_modals)

    registry.register(ToolDefinition(
        "detect_modals",
        "Detecta modais ativos: erro/confirmação/file picker/UAC/save prompt. Modais destrutivos NUNCA são aceitos automaticamente (§29/§30).",
        RiskLevel.READ_ONLY, EmptyV2Input, detect_modals,
        preflight=_local_preflight("vision", "READ_ONLY"),
    ))


def _register_adapter_tools(registry, service) -> None:
    adapters = service.adapters

    async def adapter_list(**_):
        items = []
        for adapter in adapters.all_adapters():
            detected = await asyncio.to_thread(adapter.detect)
            items.append({"app_id": adapter.app_id, "display_name": adapter.display_name,
                          "detected": detected, "capabilities": adapter.capabilities()})
        return {"success": True, "adapters": items}

    registry.register(ToolDefinition(
        "app_adapter_list",
        "Lista adapters de aplicações disponíveis (VS Code, Explorer, Windows Terminal, Chrome, Edge) com capacidades reais.",
        RiskLevel.READ_ONLY, EmptyV2Input, adapter_list,
        preflight=_local_preflight("adapters", "READ_ONLY"),
    ))

    async def app_adapter_action(app_id, action, params=None, approval_id=None, **_):
        resolution = await adapters.resolve(app_id)
        if not resolution.get("success"):
            return resolution
        adapter = adapters.by_id(app_id)
        if adapter is None:
            return {"success": False, "error_code": "ADAPTER_NOT_FOUND"}
        return await adapter.execute_action(action, dict(params or {}))

    registry.register(ToolDefinition(
        "app_adapter_action",
        "Executa ação confiável num app via adapter (ex.: vscode.open_workspace {path}, explorer.open_folder {path}, terminal.new_tab). Verificação embutida por ação.",
        RiskLevel.LOW_RISK, AdapterActionInput, app_adapter_action,
        dynamic_risk=True,
        preflight=lambda payload: {
            "risk_level": "LOW_RISK",
            "resource_key": f"adapter:{payload.get('app_id')}:{str(payload.get('action'))[:40]}",
            "host": "local",
        },
    ))

def _register_browser_v2_tools(registry, service) -> None:
    controller = service.browser_v2
    if controller is None:
        return

    async def browser_status(**_):
        return await controller.status()

    registry.register(ToolDefinition(
        "browser_status",
        "Estado do navegador gerenciado (CDP): conectado, versão, nº de abas.",
        RiskLevel.READ_ONLY, EmptyV2Input, browser_status,
        preflight=_local_preflight("browser", "READ_ONLY"),
    ))

    async def browser_select_tab(tab_id, **_):
        return await controller.select_tab(tab_id)

    registry.register(ToolDefinition(
        "browser_select_tab",
        "Torna uma aba a ativa do navegador gerenciado (Page.bringToFront).",
        RiskLevel.LOW_RISK, BrowserTabIdInput, browser_select_tab,
        preflight=_local_preflight("browser", "LOW_RISK"),
    ))

    async def browser_dom_inspect(tab_id="", max_nodes=120, **_):
        return await controller.dom_inspect(tab_id, int(max_nodes))

    registry.register(ToolDefinition(
        "browser_dom_inspect",
        "Inspeciona o DOM da página controlada via CDP. Campos password vêm mascarados; nunca expõe cookies/tokens (§60-§74).",
        RiskLevel.READ_ONLY, BrowserDomInspectInput, browser_dom_inspect,
        preflight=_local_preflight("browser", "READ_ONLY"),
    ))

    async def browser_find_element(role="", label="", text="", selector="", tab_id="", limit=10, **_):
        return await controller.find_element(role=role, label=label, text=text,
                                             selector=selector, tab_id=tab_id, limit=int(limit))

    registry.register(ToolDefinition(
        "browser_find_element",
        "Localiza elementos por role/label/text/selector na página controlada e devolve centros clicáveis.",
        RiskLevel.READ_ONLY, BrowserFindInput, browser_find_element,
        preflight=_local_preflight("browser", "READ_ONLY"),
    ))

    async def browser_click_element(selector="", x=0, y=0, tab_id="", approval_id=None, **_):
        return await controller.click_element(
            selector=selector, x=int(x), y=int(y), tab_id=tab_id,
            approval_id=approval_id,
            approvals=getattr(service.approvals, "_gate", service.approvals)
            if service.approvals else None,
        )

    registry.register(ToolDefinition(
        "browser_click_element",
        "Clique somente após approval one-use vinculado a tab, URL, identidade do alvo e selector/coordenadas; detecta navegação pós-clique.",
        RiskLevel.ELEVATED, BrowserClickElementInput, browser_click_element,
        preflight=_local_preflight("browser", "ELEVATED"),
    ))

    async def browser_type_text(text, selector="", submit=False, secret=False, tab_id="",
                                approval_id=None, **_):
        return await controller.type_text(str(text), selector=selector, submit=bool(submit),
                                          secret=bool(secret), tab_id=tab_id,
                                          approval_id=approval_id,
                                          approvals=getattr(service.approvals, "_gate", service.approvals)
                                          if service.approvals else None)

    registry.register(ToolDefinition(
        "browser_type_text",
        "Digita somente após approval one-use vinculado a tab, URL, alvo, selector, modo submit e hash do texto. secret=true mascara preview.",
        RiskLevel.ELEVATED, BrowserTypeInput, browser_type_text,
        preflight=_local_preflight("browser", "ELEVATED"),
    ))

    async def browser_select_option(text, selector="", tab_id="", approval_id=None, **_):
        return await controller.select_option(
            selector, str(text), tab_id=tab_id, approval_id=approval_id,
            approvals=getattr(service.approvals, "_gate", service.approvals)
            if service.approvals else None,
        )

    registry.register(ToolDefinition(
        "browser_select_option",
        "Seleciona <option> somente após approval one-use exato e verifica selected.",
        RiskLevel.ELEVATED, BrowserSelectOptionInput, browser_select_option,
        preflight=_local_preflight("browser", "ELEVATED"),
    ))

    async def browser_set_checked(selector, checked=True, tab_id="", approval_id=None, **_):
        return await controller.set_checked(
            selector, bool(checked), tab_id=tab_id, approval_id=approval_id,
            approvals=getattr(service.approvals, "_gate", service.approvals)
            if service.approvals else None,
        )

    registry.register(ToolDefinition(
        "browser_set_checked",
        "Marca/desmarca checkbox ou radio somente após approval one-use exato e verifica estado final (§66).",
        RiskLevel.ELEVATED, BrowserSetCheckedInput, browser_set_checked,
        preflight=_local_preflight("browser", "ELEVATED"),
    ))

    async def browser_wait_condition(condition, selector="", timeout_seconds=15.0, tab_id="", **_):
        return await controller.wait_condition(condition, selector=selector,
                                               timeout_seconds=float(timeout_seconds),
                                               tab_id=tab_id)

    registry.register(ToolDefinition(
        "browser_wait_condition",
        "Espera estruturada: navigation | element | network_idle | download (§69). Download confirma arquivo real em data/downloads (§68).",
        RiskLevel.READ_ONLY, BrowserWaitInput, browser_wait_condition,
        preflight=_local_preflight("browser", "READ_ONLY"),
    ))

    async def browser_execute_script(script, tab_id="", approval_id=None, **_):
        return await controller.execute_script(
            str(script), approval_id=approval_id,
            approvals=getattr(service.approvals, "_gate", service.approvals) if service.approvals else None,
            tab_id=tab_id,
        )

    registry.register(ToolDefinition(
        "browser_execute_script",
        "Executa JS apenas DENTRO da página controlada. Scripts com cookie/storage/fetch exigem approval_id; resultado passa por redaction (§70/§71).",
        RiskLevel.ELEVATED, BrowserScriptInput, browser_execute_script,
        dynamic_risk=True, preflight=_local_preflight("browser", "ELEVATED"),
    ))


def _register_credential_tools(registry, service) -> None:
    broker = service.credentials
    if broker is None:
        return

    async def credential_list(**_):
        return broker.list_credentials()

    registry.register(ToolDefinition(
        "credential_list",
        "Lista METADADOS de credenciais salvas (ids/kind/descrição). Nunca retorna segredos (§87).",
        RiskLevel.READ_ONLY, EmptyV2Input, credential_list,
        preflight=_local_preflight("credential", "READ_ONLY"),
    ))

    async def credential_status(credential_id, **_):
        return broker.status(credential_id)

    registry.register(ToolDefinition(
        "credential_status",
        "Status de uso de uma credencial (existe/útil/atualizada). Sem conteúdo de segredo (§88).",
        RiskLevel.READ_ONLY, CredentialIdInput, credential_status,
        preflight=_local_preflight("credential", "READ_ONLY"),
    ))
    # §89: credential_use é capability INTERNA — não registrada para o LLM.
    # §90/§91/§92: create/delete/rotate ficam na API local do operador.


def _register_elevated_tools(registry, service) -> None:
    elevated = service.elevated

    async def elevated_session_open(reason, ttl_seconds=300, approval_id=None, **_):
        return await elevated.open(reason=str(reason), ttl_seconds=int(ttl_seconds),
                                   approval_id=approval_id)

    registry.register(ToolDefinition(
        "elevated_session_open",
        "Abre sessão administrativa com UAC LEGÍTIMO único + TTL curto (não permanente). Exige approval_id (§101-§106).",
        RiskLevel.ELEVATED, ElevatedOpenInput, elevated_session_open,
        preflight=_local_preflight("elevated", "ELEVATED"),
    ))

    async def elevated_session_status(**_):
        return elevated.status()

    registry.register(ToolDefinition(
        "elevated_session_status",
        "Lista sessões elevadas ativas com expiração (§113).",
        RiskLevel.READ_ONLY, EmptyV2Input, elevated_session_status,
        preflight=_local_preflight("elevated", "READ_ONLY"),
    ))

    async def elevated_session_close(session_id, **_):
        return await elevated.close(session_id)

    registry.register(ToolDefinition(
        "elevated_session_close",
        "Encerra sessão elevada imediatamente (§114).",
        RiskLevel.LOW_RISK, CredentialIdInput if False else JobIdInput, elevated_session_close,
        preflight=_local_preflight("elevated", "LOW_RISK"),
    ))

def _register_job_tools(registry, service) -> None:
    jobs = service.jobs
    if jobs is None:
        return

    async def job_start(name, argv, working_directory="", resource_key="", **_):
        clean_argv = [str(part) for part in argv]
        return await jobs.start(str(name), clean_argv,
                                working_directory=str(working_directory),
                                resource_key=str(resource_key))

    registry.register(ToolDefinition(
        "job_start",
        "Inicia job PERSISTENTE (processo real fora do timeout de shell) para builds/downloads/backups. Retorna job_id; acompanhe com job_status (§116/§118/§135).",
        RiskLevel.LOW_RISK, JobStartInput, job_start,
        preflight=lambda payload: {
            "risk_level": "LOW_RISK",
            "resource_key": f"job:{str(payload.get('name') or 'unnamed')[:60]}",
            "host": "local",
        },
        llm_enabled=False,
    ))

    async def job_status(job_id, **_):
        return await jobs.status(job_id)

    async def job_list(**_):
        return await jobs.list()

    async def job_logs(job_id, lines=80, **_):
        return await jobs.logs(job_id, int(lines))

    async def job_cancel(job_id, **_):
        return await jobs.cancel(job_id)

    async def job_pause(job_id, **_):
        return await jobs.pause(job_id)

    async def job_resume(job_id, **_):
        return await jobs.resume(job_id)

    registry.register(ToolDefinition(
        "job_status", "Estado de um job persistente com progresso REAL extraído do output (null se inexistente — nunca inventado).", RiskLevel.READ_ONLY, JobIdInput, job_status,
        preflight=_local_preflight("job", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "job_list", "Lista jobs persistentes com estados.", RiskLevel.READ_ONLY, EmptyV2Input, job_list,
        preflight=_local_preflight("job", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "job_logs", "Tail dos logs stdout/stderr de um job (redacted, rotação automática).",
        RiskLevel.READ_ONLY, JobLogsInput if False else JobIdInput, job_logs,
        preflight=_local_preflight("job", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "job_cancel", "Cancela job em execução (kill tree) e valida cleanup (§122).", RiskLevel.LOW_RISK, JobIdInput, job_cancel,
        dynamic_risk=True, preflight=_local_preflight("job", "LOW_RISK"),
    ))
    registry.register(ToolDefinition(
        "job_pause", "Pausa job via suspensão do processo (somente quando suportado, §123).", RiskLevel.LOW_RISK, JobIdInput, job_pause,
        preflight=_local_preflight("job", "LOW_RISK"),
    ))
    registry.register(ToolDefinition(
        "job_resume", "Retoma job pausado (§124).", RiskLevel.LOW_RISK, JobIdInput, job_resume,
        preflight=_local_preflight("job", "LOW_RISK"),
    ))


def _register_task_tools(registry, service) -> None:
    tasks = service.tasks

    async def task_create(goal, steps, verification_plan="", deadline_seconds=None, **_):
        outcome = await tasks.create_task(
            str(goal), [dict(step) for step in steps],
            verification_plan=str(verification_plan or ""),
            deadline_seconds=int(deadline_seconds) if deadline_seconds else None,
        )
        if outcome.get("success"):
            await tasks.run_task(outcome["task"]["task_id"])
        return outcome

    async def task_status(task_id, **_):
        return await tasks.status(task_id)

    async def task_list(**_):
        return await tasks.list_tasks(include_terminal=False)

    async def task_cancel(task_id, **_):
        return await tasks.cancel(task_id)

    registry.register(ToolDefinition(
        "task_create",
        "Cria e inicia tarefa multi-step LONGA (plano operacional, sem chain-of-thought). Cada step chama tool real com grounding; approvals continuam valendo; progresso consultável por voz/chat (§141-§156).",
        RiskLevel.LOW_RISK, TaskCreateInput, task_create,
        preflight=_local_preflight("task", "LOW_RISK"),
    ))
    registry.register(ToolDefinition(
        "task_status", "Estado + progresso (ex.: 3/7 steps) de uma tarefa (§156).", RiskLevel.READ_ONLY, TaskIdInput, task_status,
        preflight=_local_preflight("task", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "task_list", "Lista tarefas ativas.", RiskLevel.READ_ONLY, EmptyV2Input, task_list,
        preflight=_local_preflight("task", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "task_cancel", "Cancela tarefa em execução (§157).", RiskLevel.LOW_RISK, TaskIdInput, task_cancel,
        dynamic_risk=True, preflight=_local_preflight("task", "LOW_RISK"),
    ))


def _register_watch_tools(registry, service) -> None:
    watcher = service.watcher
    if watcher is None:
        return

    async def desktop_watch(event_types, filters=None, ttl_seconds=300, **_):
        return await watcher.register([str(item) for item in event_types],
                                      filters=dict(filters or {}),
                                      ttl_seconds=int(ttl_seconds))

    async def watch_events(watch_id, after_index=0, **_):
        return watcher.events(watch_id, int(after_index))

    async def watch_list(**_):
        return watcher.status()

    async def watch_cancel(watch_id, **_):
        return await watcher.cancel(watch_id)

    registry.register(ToolDefinition(
        "desktop_watch",
        "Registra watch TEMPORÁRIO de eventos (window/process/file/service) com TTL e filtros; eventos ficam bufferizados p/ consulta (§177-§181). Evento ≠ ação.",
        RiskLevel.READ_ONLY, WatchRegisterInput, desktop_watch,
        preflight=_local_preflight("watch", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "watch_events", "Lê eventos capturados por um watch desde um índice.",
        RiskLevel.READ_ONLY, WatchEventsInput, watch_events,
        preflight=_local_preflight("watch", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "watch_list", "Lista watches ativos com TTL restante.", RiskLevel.READ_ONLY, EmptyV2Input, watch_list,
        preflight=_local_preflight("watch", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "watch_cancel", "Cancela watch antes do TTL.", RiskLevel.READ_ONLY, WatchEventsInput, watch_cancel,
        preflight=_local_preflight("watch", "READ_ONLY"),
    ))


def _register_workflow_tools(registry, service) -> None:
    workflows = service.workflows
    if workflows is None:
        return
    from app.operator.workflows import WorkflowDefinition, WorkflowStep

    async def workflow_create(workflow_id, name, steps, description="", trigger_phrases=None,
                              parameters=None, risk="LOW_RISK", **_):
        parsed_steps = []
        for raw in steps:
            parsed_steps.append(WorkflowStep(
                step_id=str(raw.get("step_id") or f"step_{len(parsed_steps)+1}"),
                tool=str(raw.get("tool")),
                params=dict(raw.get("params") or {}),
                depends_on=list(raw.get("depends_on") or []),
                description=str(raw.get("description") or "")[:200],
            ))
        definition = WorkflowDefinition(
            workflow_id=str(workflow_id), name=str(name),
            description=str(description or ""),
            trigger_phrases=[str(item)[:80] for item in (trigger_phrases or [])],
            steps=parsed_steps, parameters=dict(parameters or {}), risk=str(risk),
        )
        return await workflows.create(definition)

    async def workflow_run(workflow_id, parameters=None, **_):
        return await workflows.run(str(workflow_id), dict(parameters or {}))

    async def workflow_dry_run(workflow_id, parameters=None, **_):
        return workflows.dry_run(str(workflow_id), dict(parameters or {}))

    async def workflow_list(**_):
        return workflows.list_workflows()

    async def workflow_delete(workflow_id, **_):
        return await workflows.delete(workflow_id)

    registry.register(ToolDefinition(
        "workflow_create",
        "Salva procedimento executável estruturado (não é memória conversacional): steps validados contra o registry, versionamento automático (§189/§196-§199).",
        RiskLevel.LOW_RISK, WorkflowCreateInput, workflow_create,
        preflight=_local_preflight("workflow", "LOW_RISK"),
    ))
    registry.register(ToolDefinition(
        "workflow_run", "Executa workflow salvo: cada step passa pelo pipeline normal de grounding/approval (§191/§202/§203).",
        RiskLevel.LOW_RISK, WorkflowRunInput, workflow_run,
        dynamic_risk=True, preflight=_local_preflight("workflow", "LOW_RISK"),
    ))
    registry.register(ToolDefinition(
        "workflow_dry_run", "Prévia do plano SEM executar nada (§200).",
        RiskLevel.READ_ONLY, WorkflowRunInput, workflow_dry_run,
        preflight=_local_preflight("workflow", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "workflow_list", "Lista workflows salvos com versão.", RiskLevel.READ_ONLY, EmptyV2Input, workflow_list,
        preflight=_local_preflight("workflow", "READ_ONLY"),
    ))
    registry.register(ToolDefinition(
        "workflow_delete", "Remove workflow salvo.", RiskLevel.LOW_RISK, WorkflowRunInput, workflow_delete,
        preflight=_local_preflight("workflow", "LOW_RISK"),
    ))
