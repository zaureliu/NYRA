# Relatório Técnico — AUTONOMOUS COMPUTER OPERATOR V2

Data: 2026-08-23 · Spec: `prompt9_autonomous_computer_operator_v2.md`

## Estado inicial

```text
branch: feature/nyra-avatar-v2
HEAD: 6114452 (feat: add Voice 2.0 and desktop presence)
working tree: preservada (82 arquivos modificados do prompt8 + ~200 não rastreados)
baseline: suíte backend do prompt8 verde antes das mudanças (test_agent_loop,
          test_tool_grounding, test_turn_isolation, test_shell_security,
          test_local_operator, test_desktop_control, test_runtime_supervisor,
          test_homelab_control_plane etc.)
```

Nenhum `git reset/clean/restore/checkout` destrutivo foi executado. Nenhum
commit automático foi criado. Nada do que já estava implementado foi alterado,
excluído ou apagado — apenas estendido (routes.py/main.py/services.py receberam
adições; módulos existentes permaneceram intactos).

## Architecture

Antes: AgentController → ToolAgentLoop → tools (shell/remote/homelab/desktop/
operator/browser V1) com grounding, approvals e turn isolation.

Depois: as MESMAS camadas + `app/operator/` com 15 sub-módulos novos
(vision/adapters/browser_v2/credentials/elevated_sessions/jobs/tasks/recovery/
watcher/workflows/proactive_rules/contexts/watchdog_bridge/service/tools_reg),
43 ferramentas novas no ToolRegistry, ~30 endpoints novos sob `/api/*`,
painel Operator Activity no frontend, watchdog externo standalone em
`watchdog/nyra_watchdog.py` e flags Parte R com consumer+teste.

## Vision

Backend: captura GDI pura via ctypes (BitBlt+CAPTUREBLT para monitor/região;
PrintWindow PW_RENDERFULLCONTENT para janelas) — zero dependências novas.
Fallback order respeitado: App API/UIA primeiro (`detected_controls` projetados
da árvore UIA), pixels por último. OCR via Windows.Media.Ocr (WinRT/PowerShell)
apenas com `use_ocr=true` e UIA vazio. Frames com TTL 45s, LRU 8, ids que
expiram; cliques revalidam fingerprint (FRAME_STALE); verificação visual por
diff em grid 16px com bounding box; modais classificados (uac/error/confirm/
save/dialog) e destrutivos nunca aceitos automaticamente (exigem approval).
Testes reais: captura de Notepad ao vivo, inspeção achando MenuItem/Button/
Edit, clique com verify, PNG válido, diff sintético detectado.

## App Adapters

Implementados: VSCode (CLI-first), Explorer (open_folder/select_file com
WM_CLOSE gracioso nos testes), Windows Terminal (new_tab; execução de comando
permanece no system_shell), Chrome+Edge (wrapping CDP). Registry com aliases
resolve por id/nome. Adapters só existem onde melhoram confiabilidade; genérico
do DesktopController continua valendo para todo o resto.

## Browser

CDP sobre navegador gerenciado (perfil dedicado da NYRA). Novidades:
select_tab (bringToFront), dom_inspect (máscara `<password>` garantida),
find_element (role/label/text/selector), click_element (Input.dispatchMouseEvent
real + detecção de navegação), type_text (focus+insertText com read-back;
secret nunca ecoado), select_option/set_checked (com dispatch de eventos e
verificação), wait_condition (navigation/element/network_idle/download),
download com Browser.setDownloadBehavior allowAndName para `data/downloads` e
VERIFICAÇÃO DO ARQUIVO EM DISCO, execute_script com bloqueio fail-closed de
cookie/storage/fetch sem approval e resultado redigido. Cookies/tokens nunca
retornados. E2E real contra servidor HTTP local: 5/5 passou (inclui download
real confirmado).

## Credential Broker

Store auditado: **Windows Credential Manager** (CredReadW/CredWriteW/CredDeleteW,
CRED_TYPE_GENERIC, persist LOCAL_MACHINE) detectado e EM USO na máquina
(`backend: windows_credential_manager`); fallback **DPAPI** (CryptProtectData
com entropy dedicada) para arquivo `data/credentials-vault.bin`. LLM opera só
com credential_id (list/status = metadados). resolve()/inject_environment/
inject_header são internos. create/delete/rotate exigem approval single-use
(criação direta apenas pela API local do operador).

Prova de zero leak (testes): segredo plantado e buscado via json.dumps em
list/status/API — `assert_no_leak`; API PUT/GET/DELETE validadas; arquivo DPAPI
no disco opaco (plaintext ausente dos bytes gravados).

## Elevated Broker

Sessões persistentes (§103): uma elevação UAC legítima única inicia host
PowerShell elevado com NamedPipeServerStream local-only, DACL explícita do SID
do usuário atual, token efêmero random por sessão (nunca logado/persistido),
TTL aplicado cliente+servidor (default 300s, máx 900s, nunca permanente).
Cada comando ainda passa pelo ShellRiskClassifier; DESTRUCTIVE/CRITICAL exigem
approval próprio mesmo dentro da sessão (§112). UAC_CANCELLED tratado
honestamente. Status/close expostos; IPC é named pipe `\\.\pipe\nyra-elevated-*`
(loopback do sistema, sem rede).

## Persistent Jobs

Processos reais desanexados (DETACHED_PROCESS, argv direto, sem shell), estados
QUEUED→…→SUCCEEDED/FAILED/CANCELLED/UNKNOWN, progress SOMENTE de output real
(regex %/progress) senão null (nunca inventado), identidade pid+create_time
(rejeita reuse de PID), logs rotativos 2MB em `logs/jobs/`, persistência SQLite
(`operator_jobs`), reattach após restart da API, órfãos → FAILED/UNKNOWN,
cancel via taskkill /T /F, pause/resume via psutil suspend/resume (só quando
suportado), locks por resource_key. Testes reais: job Python longo com
progresso extraído, cancelamento com cleanup confirmado, reattach marcando
órfão.

## Task Planner

OperatorTaskManager: modelo §141 completo, estados §142, steps com
dependências/verificação/auto_rollback/transação de recovery, deadline global,
bounds (steps/retries/runtime), WAITING_FOR_USER com wakeup por approval,
WAITING_FOR_JOB aguardando job real, VERIFYING com probe configurável,
RECOVERING acionando rollback automático autorizado pelo step, retomada pós-
restart que BLOQUEIA steps destrutivos (BLOCKED, §150), persistência SQLite e
progresso "N/M steps". GroundingLedger próprio por task registra observações
dos steps. Sem chain-of-thought: só descritores operacionais. Testes: tarefa
real 3/3 steps com arquivo verificado; retry de falha transitória (2 falhas +
sucesso); cancelamento.

## Recovery

Transações action/previous_state/rollback_action/verification; backups de
arquivo (cópia com hash prévio) e snapshot textual de registry antes de mutação
de risco; rollback com hash-check que RECUSA rollback cego quando o usuário
alterou o alvo depois do snapshot (fica RECOVERY_REQUIRED); auto-rollback só
quando policy do step autoriza e snapshot confere; estados §168 completos;
caminhos protegidos fora de escopo. Testes: break temp config → detect →
rollback → verify (conteúdo restaurado byte-a-byte) e recusa do rollback cego.

## Event Watcher

Fontes: SetWinEventHook (pump único compartilhado por processo, WM_QUIT limpo)
para window.created/closed/focused/title_changed + modal.detected (#32770);
psutil snapshots APENAS com watch de processo ativo; scans de diretório APENAS
com watch de arquivo ativo; sc query APENAS para serviços assistidos. TTL
default 300s (expira sozinho), máx 16 watches, debounce 0.3s, buffer por watch,
publicação DESKTOP_EVENT no bus. device.* responde CAPABILITY_UNAVAILABLE
honestamente. Event ≠ ação: watcher só observa; rules/tasks consomem. Testes:
file.created real, process.exited real, TTL/LIMIT.

## Workflow Memory

CRUD + versionamento incremental obrigatório em update, validação contra o
registry (tool desconhecida recusa salvamento), dry_run sem executar nada,
parâmetros `{nome}` com falha fechada MISSING_PARAMETERS, trigger_phrases com
matcher, execução step-a-step pelo pipeline normal (grounding/approval
preservados) com relatório parcial em falha, store JSON atômico
(`data/workflows.json`). Teste Z real: criar "Abrir ambiente teste" → validar →
v2 → dry-run → run com arquivo verificado → reload do store → trigger match.

## Watchdog

Standalone stdlib (sem LLM/Ollama), intervalo 10s, checks backend HTTP /
frontend TCP / Ollama HTTP / processo desktop, threshold 3 falhas → restart
(backend embutido via uvicorn desanexado; demais componentes opt-in por env),
RESTART_LIMIT=3 por janela de 600s → CRASH_LOOP_PROTECTED, heartbeat JSON
(lido pela API `/api/watchdog/status` com staleness 30s), canal one-shot
`data/watchdog-requests/*.json` consumido (bridge subscreve RUNTIME_FAILED/
RUNTIME_CRASH_LOOP do nyra_backend — §227), log separado `logs/watchdog.log`,
sem admin, sem self-update.

## Proactive Operator

Default OFF (`NYRA_PROACTIVE_OPERATOR_ENABLED=false`, validado em teste).
Regras cadastradas (schema Pydantic) com allowlist de ações {notify,
run_workflow, open_report} — ação destrutiva não solicitada é IMPOSSÍVEL
(§239). Orçamento/cooldown continuam no ProactiveEngine existente.

## Context Isolation

TaskContext/JobContext/WatchContext/WorkflowContext registrados com kind;
`registry.get(id, expected_kind)` lança CrossContextRejectionError em uso
cruzado (§250) e KeyError para id inexistente; expiry/TTL varre registry;
TurnContext segue intocado em TurnRegistry. IDs distintos: turn_/task_/job_/
watch_/wf_. Testes de separação e rejeição cruzada.

## UI / Observability

Painel compacto `OperatorActivityPanel.tsx` (dashboard + integrações): tarefas
com "N/M steps", jobs ativos com progresso real, contagem de watches/workflows
e estado do watchdog por heartbeat. Poll 5s, sem chain-of-thought, sem
dashboard gigante (§271).

## Tests

```text
baseline backend: prompt8 verde (379 total incluindo novos)
new tests:        52 casos em 7 arquivos (vision/adapters+credentials+jobs+
                  recovery+watcher/tasks+workflows/watchdog+elevated/browser_v2/api)
backend final:    379 passed, 0 failed (~3m17s)
frontend vitest:  17 files / 53 tests passed
frontend build:   tsc -b && vite build OK (189ms)
Vite dev/Tauri:   src-tauri não alterado nesta fase (sem rebuild Rust necessário)
git diff --check: LIMPO (sem whitespace errors)
```

Suítes novas: `test_operator_vision.py`, `test_operator_adapters_credentials.py`,
`test_operator_jobs_recovery_watcher.py`, `test_operator_tasks_workflows.py`,
`test_operator_watchdog_elevated.py`, `test_operator_browser_v2.py`,
`test_operator_v2_api.py`.

## Performance (idle)

Medição in-loco (psutil, 6 amostras de 1s, app real via lifespan):

```text
app SEM operator v2 (watcher+jobs off): 17.53% CPU, 25 threads
app COM operator v2 completo:           15.88% CPU, 25 threads
overhead incremental V2:                ≈ 0 (dentro do ruído ±1.7%)
```

Os loops novos são near-no-op em idle: job monitor 2s (SQLite barato), watcher
dispatch 1.5s com early-return sem watches, sweep 5s, vision on-demand (nunca
captura continuamente — recusa scope desktop por padrão), watchdog externo
nem estava rodando (fora do processo; loop 10s ≈ zero CPU). Baseline absoluto
(~16-17%) vem de subsistemas pré-existentes (perception 0.5s, attention decay,
homelab/runtime monitors) e não é regressão desta fase.

## Security

```text
arbitrary CMD: SIM            persistent jobs: SIM
arbitrary PowerShell: SIM     workflow memory: SIM
UI Automation: SIM            event watcher: SIM
vision fallback: SIM          self-healing: SIM
browser control: SIM          watchdog: SIM
credential broker: SIM        legitimate UAC: SIM
UAC bypass: NÃO               credential theft: NÃO
secret exposure to LLM: NÃO   grounding: SIM
verification: SIM             audit: SIM
```

Adicionalmente: approval single-use mantido em TODOS os novos fluxos de risco;
modais destrutivos nunca auto-aceitos; password fields mascarados no DOM e no
visual; cookies/tokens jamais lidos; scripts de página com APIs sensíveis
exigem approval; components NYRA protegidos (taskkill de jobs nunca toca
processos próprios — herança das proteções existentes).

## Critérios de aceite (§320-§324)

- Multi-app/multi-step real: VALIDADO por testes (browser→download→verify em
  disco; tasks 3/3 com probes; workflows executados com grounding).
- Event-driven ("quando terminar…"): MECANISMO validado (watches file/process
  reais + TASK_WAITING_FOR_JOB + regras proativas); automação ponta-a-ponta
  por voz depende de hardware/microfone da sessão.
- Workflow por frase: VALIDADO (trigger match + run).
- Watchdog revive backend morto: LÓGICA e CANAL validados por testes; restart
  real do backend de produção NÃO foi executado durante a sessão (§309 — não
  matar a sessão de desenvolvimento sem plano). Executar manualmente:
  `python watchdog\nyra_watchdog.py` e derrubar o backend.
- Tarefa longa sem travar o chat: VALIDADO (jobs/tasks rodam fora de tool calls;
  status consultável a qualquer momento).

## Pendências

bugs: nenhum conhecido aberto desta fase.

limitations:
- OCR depende de idioma instalado no Windows (TryCreateFromUserProfileLanguages);
  fallback honesto `available:false`.
- device.connected/disconnected sem WM_DEVICECHANGE nesta versão (resposta
  honesta CAPABILITY_UNAVAILABLE).
- Restart real do backend pelo watchdog não demonstrado in-session (proteção
  da sessão de dev); coberto por testes unitários do guard + bridge.
- src-tauri não recompilado (nenhuma mudança Rust nesta fase).

environment blockers:
- UAC real exige interação humana no desktop — testes de sessão elevada
  fail-closed sem approval (por design); abrir sessão real é manual.
- Voice E2E (§310-§311) requer microfone/sessão de áudio ativa.

future improvements:
- Marshalar eventos CDP (Network.responseReceived) para downloads parciais.
- WinEvent hook por-watch com filtros nativos (hoje fila global filtrada local).
- Sincronizar workflow runs com Agent Runs (run_id por step) no histórico.
- Painel de credentials no Settings (API já pronta).
