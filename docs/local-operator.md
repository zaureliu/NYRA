# NYRA Local Operator V1

Camada que transforma a NYRA em **operadora local do Windows** — além de shell e app launcher: janelas, UI Automation, input, filesystem, processos, serviços, registro, tarefas agendadas, navegador e energia. O AgentController continua sendo o único cérebro; tudo aqui são capabilities com schema Pydantic, risco, approval, grounding e verificação.

## Módulos

| Arquivo | Responsabilidade |
|---|---|
| `backend/app/desktop/control.py` | Ciclo de vida de apps: launch/find/status + operações de janela (focus/minimize/maximize/restore/move/resize/close) com ACT→VERIFY |
| `backend/app/desktop/window_manager.py` | Win32 puro: SetForegroundWindow/ShowWindow/SetWindowPos/WM_CLOSE + releitura de estado para confirmar efeito; guarda de processos NYRA |
| `backend/app/desktop/uia.py` | UI Automation via comtypes/UIAutomationCore: inspect/find/click(InvokePattern)/set_text(ValuePattern+releitura)/get_text/send_keys(SendInput com foreground verify) |
| `backend/app/desktop/discovery.py` | Descoberta dinâmica de apps (App Paths, PATH, Start Menu, Get-StartApps/UWP) |
| `backend/app/desktop/operator.py` | Filesystem/processos/serviços/registro/tarefas/energia com approval single-use e elevação UAC legítima |
| `backend/app/desktop/browser.py` | Navegador gerenciado via Chrome DevTools Protocol (perfil próprio), abas/navegação verificadas |

## Tools

**Desktop/janelas**: `desktop_list_apps`, `desktop_windows`, `desktop_find_application`, `desktop_open_application`, `desktop_launch`, `desktop_focus`, `desktop_close`, `desktop_minimize`, `desktop_maximize`, `desktop_restore`, `desktop_move_window`, `desktop_resize_window`, `desktop_open_file`, `desktop_open_url`.

**UI Automation** (flag `NYRA_DESKTOP_UI_AUTOMATION_ENABLED`; send_keys também exige `NYRA_DESKTOP_INPUT_FALLBACK_ENABLED`): `ui_inspect`, `ui_find`, `ui_click`, `ui_set_text`, `ui_get_text`, `ui_send_keys`.

**Operador local** (master flag `NYRA_LOCAL_OPERATOR_ENABLED`):
- filesystem: `filesystem_list/read/write/copy/move/rename/delete/mkdir/search`
- processos: `process_list/status/start/stop`
- serviços: `windows_service_list/status/start/stop/restart`
- registro: `registry_read/set`
- tarefas: `task_list/run/delete`
- energia: `system_power` (lock/sleep/logoff/restart/shutdown)
- browser CDP: `browser_open/navigate/tabs/close_tab/refresh/back/forward`

## Segurança

- Fechar janelas usa **WM_CLOSE gracioso**; taskkill nunca é primeira opção. Documento não salvo → diálogo detectado, NYRA **não descarta** sozinha.
- Componentes NYRA (backend, presence Tauri) são **protegidos**: close/stop neles é bloqueado.
- Mutações sensíveis exigem `approval_id` de uso único (mesmo ShellApprovalGate); serviços/registro elevam por `runas`/UAC real (sem bypass, sem credenciais).
- `filesystem_delete` bloqueia raízes/home/projeto; `system_power` shutdown/restart dão 30s de cancelamento (`shutdown /a`).
- Browser CDP roda em **perfil dedicado**; cookies/tokens nunca saem das chamadas.

## Encoding (§162–§175)

Fontes são UTF-8; auditor automático em `backend/app/core/encoding_audit.py` detecta dupla codificação/replacement chars. Teste de regressão: `backend/tests/test_encoding.py` (falha a suíte se mojibake voltar a aparecer em strings de UI).
