# KAZUMI — Autonomous Computer Operator V2

Fase implementada sobre o estado validado do prompt8 (spec:
`prompt9_autonomous_computer_operator_v2.md`). O Agent Controller continua
sendo o único cérebro; tudo aqui são **capabilities** com schema Pydantic,
risco, approval single-use, grounding e verificação.

## Arquitetura

```text
USER / VOICE
      ↓
TURN CONTEXT
      ↓
KAZUMI AGENT
      ↓
OPERATOR V2 (backend/app/operator/)
      ├── vision.py            Screen Understanding (UIA-first, OCR fallback)
      ├── adapters.py          App Adapters (vscode/explorer/terminal/chrome/edge)
      ├── browser_v2.py        CDP: DOM/find/click/type/select/check/wait/download/script
      ├── clipboard.py         status metadata-only + write/clear verificados por Win32
      ├── credentials.py       Credential Broker (Credential Manager/DPAPI)
      ├── elevated_sessions.py Sessão admin TTL via UAC legítimo único + pipe local
      ├── jobs.py              Jobs persistentes (reattach/orphan/logs rotativos)
      ├── tasks.py             Task Planner V2 multi-step (estados completos)
      ├── recovery.py          Backups/transações/rollback (nunca cego)
      ├── watcher.py           WinEvent hook + watchers escopados com TTL
      ├── workflows.py         Memória de procedimentos versionada
      ├── proactive_rules.py   Regras proativas (default OFF)
      ├── contexts.py          Task/Job/Watch/Workflow Context + cross-context rejection
      ├── watchdog_bridge.py   Runtime Supervisor → request de restart externo (§227)
      └── service.py / tools_reg.py   fachada + registro das ferramentas
      ↓
GROUNDING → VERIFY → REPORT
```

## Flags (Parte R) — todas com consumer e teste

| Env | Default | Consumer |
|---|---|---|
| `KAZUMI_VISION_ENABLED` | true | `OperatorV2Service` (vision tools/API) |
| `KAZUMI_VISION_FRAME_TTL_SECONDS` | 45 | `FrameStore` |
| `KAZUMI_VISION_DEBUG_KEEP_FRAMES` | false | persistência opt-in de PNG |
| `KAZUMI_BROWSER_CONTROL_ENABLED` | true | browser v2 + adapters chrome/edge |
| `KAZUMI_CREDENTIAL_BROKER_ENABLED` | true | broker + endpoints `/api/credentials` |
| `KAZUMI_PERSISTENT_JOBS_ENABLED` | true | job manager + monitor |
| `KAZUMI_WORKFLOW_ENGINE_ENABLED` | true | workflow engine/tools |
| `KAZUMI_DESKTOP_WATCHER_ENABLED` | true | watcher |
| `KAZUMI_WATCH_DEFAULT_TTL_SECONDS` | 300 | TTL default de watches |
| `KAZUMI_WATCHDOG_ENABLED` | true | leitura do heartbeat no backend |
| `KAZUMI_PROACTIVE_OPERATOR_ENABLED` | **false** | ProactiveOperator |
| `KAZUMI_ELEVATED_SESSION_DEFAULT_TTL_SECONDS` | 300 | ElevatedSessionManager |

## Ferramentas novas (LLM)

Vision: `screen_capture`, `visual_inspect`, `visual_click`, `visual_type`,
`visual_read`, `screen_diff`, `detect_modals`.
Adapters: `app_adapter_list`, `app_adapter_action`.
Browser V2: `browser_status/select_tab/dom_inspect/find_element/click_element/
type_text/select_option/set_checked/wait_condition/execute_script`.
Clipboard: `clipboard_status` (`READ_ONLY`, somente metadados),
`clipboard_write_text` e `clipboard_clear` (`LOW_RISK`). A resposta nunca lê nem
ecoa o conteúdo; escrita/limpeza são verificadas por formato e sequence Win32.
Credentials (metadata-only): `credential_list`, `credential_status` — uso/
create/delete/rotate ficam internos ou na API local (o LLM NUNCA vê segredo).
Elevated: `elevated_session_open/close/status` (+ execute interno).
Jobs: `job_status/list/logs/cancel/pause/resume`. `job_start` permanece apenas como compatibilidade de API, desabilitado para LLM e REST; criação arbitrária deve passar por `system_shell`.
Tasks: `task_create/status/list/cancel`.
Watches: `desktop_watch/watch_events/watch_list/watch_cancel`.
Workflows: `workflow_create/run/dry_run/list/delete`.

## API (Parte Q)

`/api/operator/v2/status`, `/api/vision/{capture,frames}`, `/api/adapters`,
`/api/credentials{,/id/status}` PUT/DELETE para criar/rotacionar/remover
(operador direto), `/api/elevated/session/*`, `/api/jobs*`, `/api/tasks*`,
`/api/watches*`, `/api/workflows*`, `/api/watchdog/status`.

## Watchdog externo (Parte L)

`watchdog/kazumi_watchdog.py` — stdlib pura, sem Ollama. Checks: backend HTTP,
frontend TCP, Ollama HTTP, processo desktop. Restart com limite por janela
(crash-loop protection), heartbeat em `data/watchdog-heartbeat.json`
(lido por `/api/watchdog/status`) e canal one-shot `data/watchdog-requests/`
consumido pelo bridge do Runtime Supervisor. Log separado em
`logs/watchdog.log`. Rodar com: `python watchdog\kazumi_watchdog.py`.

## Garantias preservadas

UAC legítimo (sem bypass), approval single-use para DESTRUCTIVE+, risk
classifier em todo comando, grounding ledger por task/turn, cross-context
rejection, turn isolation intocado, redaction em toda saída, zero exposição
de cookies/tokens/passwords ao LLM, nenhum conteúdo de clipboard em logs,
eventos ou Agent Run (redigido antes do fingerprint persistente), componentes
KAZUMI protegidos.
