"""Desktop control native tools with grounding fields and risk mapping."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.tools.models import RiskLevel
from app.tools.registry import ToolDefinition

if TYPE_CHECKING:
    from app.desktop.control import DesktopController


class DesktopAppInput(BaseModel):
    app: str = Field(
        min_length=2,
        max_length=80,
        pattern=r"^[\w\s.\-()&+]+$",
        description="ID ou nome exato de um aplicativo confiável do Desktop Apps Registry.",
    )


class DesktopOptionalAppInput(BaseModel):
    app: str = Field(default="", max_length=64, pattern=r"^[a-z0-9_]*$")


class DesktopQueryInput(BaseModel):
    query: str = Field(
        min_length=2,
        max_length=80,
        pattern=r"^[\w\s.\-()&+]+$",
        description="Nome livre do aplicativo instalado (ex.: 'Wireshark', 'bloco de notas').",
    )


class DesktopWindowStatusInput(BaseModel):
    app: str = Field(default="", max_length=64, pattern=r"^[a-z0-9_]*$")
    query: str = Field(default="", max_length=80, pattern=r"^[\w\s.\-()&+]*$")


class DesktopWindowTargetInput(BaseModel):
    app: str = Field(default="", max_length=64, pattern=r"^[a-z0-9_]*$")
    query: str = Field(default="", max_length=80, pattern=r"^[\w\s.\-()&+]*$")
    hwnd: int | None = Field(default=None, ge=1)


class DesktopMoveInput(DesktopWindowTargetInput):
    x: int = Field(ge=-8192, le=8192)
    y: int = Field(ge=-8192, le=8192)


class DesktopResizeInput(DesktopWindowTargetInput):
    width: int = Field(ge=200, le=8192)
    height: int = Field(ge=140, le=8192)


class DesktopOpenFileInput(BaseModel):
    path: str = Field(min_length=2, max_length=500)
    app: str = Field(default="", max_length=64, pattern=r"^[a-z0-9_]*$")


class DesktopOpenUrlInput(BaseModel):
    url: str = Field(min_length=4, max_length=800)


class UiTargetInput(BaseModel):
    app: str = Field(default="", max_length=64, pattern=r"^[a-z0-9_]*$")
    query: str = Field(default="", max_length=80, pattern=r"^[\w\s.\-()&+]*$")
    hwnd: int | None = Field(default=None, ge=1)


class UiFindInput(UiTargetInput):
    name: str = Field(default="", max_length=120)
    automation_id: str = Field(default="", max_length=120)
    control_type: str = Field(default="", max_length=40, pattern=r"^[A-Za-z]*$")
    class_name: str = Field(default="", max_length=120)


class UiClickInput(UiTargetInput):
    name: str = Field(default="", max_length=120)
    automation_id: str = Field(default="", max_length=120)
    control_type: str = Field(default="", max_length=40, pattern=r"^[A-Za-z]*$")
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)


class UiSetTextInput(UiClickInput):
    value: str = Field(min_length=1, max_length=20000)


class UiSendKeysInput(UiTargetInput):
    text: str = Field(min_length=1, max_length=20000)
    approval_id: str | None = Field(default=None, min_length=16, max_length=128)


def register_desktop_tools(
    registry,
    controller: "DesktopController",
    *,
    uia_enabled: bool = True,
    input_fallback_enabled: bool = True,
) -> None:
    async def desktop_list_apps() -> dict:
        return controller.list_apps()

    async def desktop_launch(app: str) -> dict:
        return await controller.launch(app, origin="tool")

    async def desktop_windows(app: str = "", query: str = "") -> dict:
        return controller.status_windows(app or None, query or None)

    async def desktop_find_application(query: str) -> dict:
        return controller.find(query)

    async def desktop_open_application(query: str) -> dict:
        return await controller.launch_dynamic(query, origin="tool")

    read_only_preflight = lambda payload: {"risk_level": "READ_ONLY", "resource_key": f"desktop:{payload.get('app', '')}{payload.get('query', '')}", "host": "local"}
    open_preflight = lambda payload: {"risk_level": "LOW_RISK", "resource_key": f"desktop:open:{payload.get('query', '')}", "host": "local"}

    def window_action_preflight(action: str):
        return lambda payload: {
            "risk_level": "LOW_RISK",
            "resource_key": f"desktop:{action}:{payload.get('app', '') or payload.get('query', '') or payload.get('hwnd', '')}",
            "host": "local",
        }

    registered_ids = ", ".join(spec.id for spec in controller.registry.valid_specs()) or "nenhum"

    async def desktop_focus(app="", query="", hwnd=None, **_):
        return await controller.focus(app=app, query=query, hwnd=hwnd)

    async def desktop_close(app="", query="", hwnd=None, **_):
        return await controller.close(app=app, query=query, hwnd=hwnd)

    async def desktop_minimize(app="", query="", hwnd=None, **_):
        return await controller.minimize(app=app, query=query, hwnd=hwnd)

    async def desktop_maximize(app="", query="", hwnd=None, **_):
        return await controller.maximize(app=app, query=query, hwnd=hwnd)

    async def desktop_restore(app="", query="", hwnd=None, **_):
        return await controller.restore(app=app, query=query, hwnd=hwnd)

    async def desktop_move_window(x, y, app="", query="", hwnd=None, **_):
        return await controller.move_window(int(x), int(y), app=app, query=query, hwnd=hwnd)

    async def desktop_resize_window(width, height, app="", query="", hwnd=None, **_):
        return await controller.resize_window(int(width), int(height), app=app, query=query, hwnd=hwnd)

    async def desktop_open_file(path, app="", **_):
        return await controller.open_file(path, app=app)

    async def desktop_open_url(url, **_):
        return await controller.open_url(url)

    registry.register(ToolDefinition(
        "desktop_list_apps",
        "Lista aplicativos desktop registrados no Desktop Apps Registry com janelas visíveis atuais de cada um.",
        RiskLevel.READ_ONLY, DesktopOptionalAppInput,
        lambda app="", **_: desktop_windows(app) if app else desktop_list_apps(),
        dynamic_risk=False, llm_enabled=True, preflight=read_only_preflight,
    ))
    registry.register(ToolDefinition(
        "desktop_windows",
        "Consulta janelas visíveis AGORA; aceita id do registry (app) ou texto livre (query, ex.: 'bloco de notas', 'chrome'). Use antes de afirmar que algo está aberto.",
        RiskLevel.READ_ONLY, DesktopWindowStatusInput,
        lambda app="", query="", **_: desktop_windows(app, query),
        dynamic_risk=False, llm_enabled=True, preflight=read_only_preflight,
    ))
    registry.register(ToolDefinition(
        "desktop_find_application",
        "Localiza aplicativos instalados por nome livre (App Paths, PATH, Start Menu, Get-StartApps/UWP). READ_ONLY; não abre nada. Retorna candidatos ou AMBIGUOUS para perguntar ao operador.",
        RiskLevel.READ_ONLY, DesktopQueryInput,
        lambda query, **_: desktop_find_application(query),
        dynamic_risk=False, llm_enabled=True, preflight=read_only_preflight,
    ))
    registry.register(ToolDefinition(
        "desktop_open_application",
        "Abre QUALQUER aplicativo instalado pelo nome livre usando o resolver dinâmico e só retorna sucesso após confirmar janela visível real. Prefira esta tool a Start-Process via system_shell para abrir apps.",
        RiskLevel.LOW_RISK, DesktopQueryInput,
        lambda query, **_: desktop_open_application(query),
        dynamic_risk=True, llm_enabled=True, preflight=open_preflight,
    ))
    registry.register(ToolDefinition(
        "desktop_launch",
        f"Abre aplicativo desktop registrado por id ou nome humano. IDs válidos: {registered_ids}. Só retorna sucesso após confirmar janela visível real.",
        RiskLevel.LOW_RISK, DesktopAppInput,
        lambda app: desktop_launch(app),
        dynamic_risk=True, llm_enabled=True,
        preflight=lambda payload: {
            "risk_level": "LOW_RISK",
            "resource_key": f"desktop:{controller.resolve_registered_app_id(str(payload.get('app', ''))) or payload.get('app', '')}",
            "host": "local",
        },
    ))

    target_hint = "Alvo: id do registry (app), texto livre (query, ex.: 'bloco de notas') ou hwnd."
    registry.register(ToolDefinition(
        "desktop_focus",
        f"Traz uma janela para o primeiro plano e confirma via GetForegroundWindow. {target_hint}",
        RiskLevel.LOW_RISK, DesktopWindowTargetInput, desktop_focus,
        dynamic_risk=False, llm_enabled=True, preflight=window_action_preflight("focus"),
    ))
    registry.register(ToolDefinition(
        "desktop_close",
        f"Fecha UMA janela graciosamente via WM_CLOSE (nunca taskkill como primeira opção) e verifica que ela sumiu. {target_hint}",
        RiskLevel.LOW_RISK, DesktopWindowTargetInput, desktop_close,
        dynamic_risk=False, llm_enabled=True, preflight=window_action_preflight("close"),
    ))
    registry.register(ToolDefinition(
        "desktop_minimize",
        f"Minimiza janela e confirma via IsIconic. {target_hint}",
        RiskLevel.LOW_RISK, DesktopWindowTargetInput, desktop_minimize,
        dynamic_risk=False, llm_enabled=True, preflight=window_action_preflight("minimize"),
    ))
    registry.register(ToolDefinition(
        "desktop_maximize",
        f"Maximiza janela e confirma via IsZoomed. {target_hint}",
        RiskLevel.LOW_RISK, DesktopWindowTargetInput, desktop_maximize,
        dynamic_risk=False, llm_enabled=True, preflight=window_action_preflight("maximize"),
    ))
    registry.register(ToolDefinition(
        "desktop_restore",
        f"Restaura janela minimizada/maximizada ao estado normal. {target_hint}",
        RiskLevel.LOW_RISK, DesktopWindowTargetInput, desktop_restore,
        dynamic_risk=False, llm_enabled=True, preflight=window_action_preflight("restore"),
    ))
    registry.register(ToolDefinition(
        "desktop_move_window",
        f"Move janela para coordenadas x,y e confirma pela posição real. {target_hint}",
        RiskLevel.LOW_RISK, DesktopMoveInput, desktop_move_window,
        dynamic_risk=False, llm_enabled=True, preflight=window_action_preflight("move"),
    ))
    registry.register(ToolDefinition(
        "desktop_resize_window",
        f"Redimensiona janela para width×height e confirma pelo rect real. {target_hint}",
        RiskLevel.LOW_RISK, DesktopResizeInput, desktop_resize_window,
        dynamic_risk=False, llm_enabled=True, preflight=window_action_preflight("resize"),
    ))
    registry.register(ToolDefinition(
        "desktop_open_file",
        "Abre um arquivo local por associação do Windows ou em aplicativo registrado (app=id do registry). Caminho é normalizado e validado antes.",
        RiskLevel.LOW_RISK, DesktopOpenFileInput, desktop_open_file,
        dynamic_risk=False, llm_enabled=True,
        preflight=lambda payload: {"risk_level": "LOW_RISK", "resource_key": f"desktop:file:{payload.get('path', '')[:60]}", "host": "local"},
    ))
    registry.register(ToolDefinition(
        "desktop_open_url",
        "Abre somente URL HTTP/HTTPS absoluta no navegador padrão. file:, shell:, ms-settings: e credenciais embutidas são bloqueados.",
        RiskLevel.LOW_RISK, DesktopOpenUrlInput, desktop_open_url,
        dynamic_risk=False, llm_enabled=True,
        preflight=lambda payload: {"risk_level": "LOW_RISK", "resource_key": f"desktop:url:{payload.get('url', '')[:60]}", "host": "local"},
    ))

    # ---------------- UI Automation (accessibility-first)

    async def ui_inspect(app="", query="", hwnd=None, max_depth=5, **_):
        return await controller.ui_inspect(app=app, query=query, hwnd=hwnd, max_depth=int(max_depth))

    async def ui_find(name="", automation_id="", control_type="", class_name="", app="", query="", hwnd=None, **_):
        return await controller.ui_find(
            name=name, automation_id=automation_id, control_type=control_type, class_name=class_name,
            app=app, query=query, hwnd=hwnd,
        )

    async def ui_click(name="", automation_id="", control_type="", app="", query="", hwnd=None,
                       approval_id=None, **_):
        return await controller.ui_click(
            name=name, automation_id=automation_id, control_type=control_type,
            app=app, query=query, hwnd=hwnd, approval_id=approval_id,
        )

    async def ui_set_text(value, name="", automation_id="", control_type="", app="", query="", hwnd=None,
                          approval_id=None, **_):
        return await controller.ui_set_text(
            value, name=name, automation_id=automation_id, control_type=control_type,
            app=app, query=query, hwnd=hwnd, approval_id=approval_id,
        )

    async def ui_get_text(name="", automation_id="", control_type="", app="", query="", hwnd=None, **_):
        return await controller.ui_get_text(
            name=name, automation_id=automation_id, control_type=control_type,
            app=app, query=query, hwnd=hwnd,
        )

    async def ui_send_keys(text, app="", query="", hwnd=None, approval_id=None, **_):
        return await controller.ui_send_keys(
            text, app=app, query=query, hwnd=hwnd, approval_id=approval_id,
        )

    uia_preflight = lambda payload: {"risk_level": "READ_ONLY", "resource_key": "ui:read", "host": "local"}

    if not uia_enabled:
        return

    registry.register(ToolDefinition(
        "ui_inspect",
        "Inspeciona a árvore de acessibilidade (UIA) da janela alvo: nomes, AutomationIds, tipos de controle e valores. Use antes de clicar/digitar para descobrir os controles.",
        RiskLevel.READ_ONLY, UiTargetInput, ui_inspect,
        dynamic_risk=False, llm_enabled=True, preflight=uia_preflight,
    ))
    registry.register(ToolDefinition(
        "ui_find",
        "Busca controles dentro da janela alvo por nome/automation_id/control_type/class_name (somente leitura).",
        RiskLevel.READ_ONLY, UiFindInput, ui_find,
        dynamic_risk=False, llm_enabled=True, preflight=uia_preflight,
    ))
    registry.register(ToolDefinition(
        "ui_click",
        "Clica num controle estruturado somente após approval one-use vinculado ao HWND e à identidade exata do elemento.",
        RiskLevel.ELEVATED, UiClickInput, ui_click,
        dynamic_risk=False, llm_enabled=True,
        preflight=lambda payload: {
            "risk_level": "ELEVATED",
            "resource_key": f"ui:click:{payload.get('hwnd', '')}:{str(payload.get('automation_id') or payload.get('name') or '')[:40]}",
            "host": "local",
        },
    ))
    registry.register(ToolDefinition(
        "ui_set_text",
        "Preenche um campo somente após approval one-use vinculado ao HWND, alvo e hash do valor; relê o conteúdo para confirmar.",
        RiskLevel.ELEVATED, UiSetTextInput, ui_set_text,
        dynamic_risk=False, llm_enabled=True,
        preflight=lambda payload: {
            "risk_level": "ELEVATED",
            "resource_key": f"ui:set_text:{payload.get('hwnd', '')}:{str(payload.get('automation_id') or payload.get('name') or '')[:40]}",
            "host": "local",
        },
    ))
    registry.register(ToolDefinition(
        "ui_get_text",
        "Lê o conteúdo acessível de um controle da janela alvo.",
        RiskLevel.READ_ONLY, UiFindInput, ui_get_text,
        dynamic_risk=False, llm_enabled=True, preflight=uia_preflight,
    ))
    if input_fallback_enabled:
        registry.register(ToolDefinition(
            "ui_send_keys",
            "Fallback de teclado sensível: exige approval one-use vinculado ao HWND e ao hash das teclas, confirma foreground e só então envia.",
            RiskLevel.ELEVATED, UiSendKeysInput, ui_send_keys,
            dynamic_risk=False, llm_enabled=True,
            preflight=lambda payload: {
                "risk_level": "ELEVATED",
                "resource_key": f"ui:send_keys:{payload.get('hwnd', '')}:{str(payload.get('query') or payload.get('app') or '')[:40]}",
                "host": "local",
            },
        ))
