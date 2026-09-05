# KAZUMI — Validação Final Release Candidate V1

Consolidado da execução `KAZUMI_FINAL_RELEASE_CANDIDATE_CLOSURE_V1` — último
ciclo de desenvolvimento antes do freeze funcional.

## 1. Baseline

```text
branch:      feature/kazumi-avatar-v2
HEAD:        6114452 (feat: add Voice 2.0 and desktop presence)
versão:      0.2.0 (unificada backend/frontend/Tauri/pyproject)
modelo:      qwen3:8b (oficial, sem promoções)
runtime:     backend via runtime oficial .venv\Scripts\python.exe na :8000,
             watchdog externo puro-stdlib, Task Scheduler como supervisor
             de processo nesta estação (auto-restart x10)
```

Nenhum commit/tag/release foi criado pela execução (working tree preservado).

## 2. Arquitetura final (sem subsistemas paralelos)

Todos os subsistemas consolidados foram preservados: Conversation Engine,
Ollama Warm Manager, GroundingLedger, Turn Isolation, Agent Controller,
Task Planner, Persistent Jobs, Workflow Engine, Recovery, Runtime Supervisor,
Local Operator, Desktop Control/UIA, Vision, Browser V2, Credential Broker,
Elevated Broker, Homelab Control Plane, integrações HA/Proxmox/OpenWrt/
Sentinel, Operations UI V3, Voice V3 + bridge externa, Desktop Presence,
EventBus/WebSocket, Tauri, Watchdog.

Adições cirúrgicas desta closure:

| Arquivo | Papel |
|---|---|
| `backend/app/core/lifecycle.py` | Shutdown/restart coordenados, flag intencional, disarm do watchdog |
| `scripts/restart-session.ps1` | Espera porta livre → launcher oficial → nova sessão |
| `backend/app/api/routes.py` `/api/runtime/power/{shutdown,restart}` | Power do operador (UI-only) |
| `backend/app/api/routes.py` `/api/release/revalidate` | Gate em background job |
| `backend/app/core/release_info.py` | Freshness/STALE por artefato + git_head + revalidation |
| `scripts/release_gate.py` | Publica progresso N/M (`release-gate-progress.json`) + git_head no relatório |
| `frontend/src/components/ConversationPanel.tsx` | Agrupador de tools por Agent Run + modo técnico persistido |
| `frontend/src/ops/pages/SettingsPageV3.tsx` | Botões Encerrar/Reiniciar KAZUMI com confirmação |
| `frontend/src/ops/pages/AboutPage.tsx` | STALE/timestamp/Revalidar sem congelar UI |
| `watchdog/kazumi_watchdog.py` | Canal one-shot `shutdown` (disarm antes do backend sair); requests avaliados ANTES das decisões de health |
| `backend/tests/conftest.py` | Suíte hermética contra estado persistido do operador |

## 3. Bugs reais corrigidos

| Bug | Causa raiz | Correção | Verificação |
|---|---|---|---|
| 8 testes falhando (agent_loop/operator_v2/remote_shell/runtime_supervisor/tool_grounding) | Suíte lia estado real do operador (`agent_read_only=true`, `proactive_operator_enabled=true` em settings-v33.json) | Env defaults herméticos no conftest; overrides explícitos vencem | 515 passed / 0 fail |
| Release Readiness RED com artefato velho | Sem metadados de frescor; leitura de chave errada (`state` vs `verdict`) | classify() STALE >12h ou sem timestamp; STALE nunca gera RED; chave corrigida | test_release_health_* (3 casos) |
| Watchdog podia relançar durante shutdown intencional | Requests consumidos DEPOIS das decisões de health | `_consume_requests` movido para o topo do ciclo; ação `shutdown` encerra o watchdog sem relançar | E2E §10 (abaixo) |
| Watchdog usava Python global como fallback silencioso | `sys.executable` fallback em restart_backend | Fallback removido: sem .venv oficial, não relança (§9.2) | código + log "relançamento ABORTADO" |
| `process_running` quebrava (bytes.casefold) | tasklist stdout bytes | decode defensivo | watchdog ciclando estável |
| Mensagem agregada contraditória ("NENHUM_HOST_ALCANÇÁVEL" com host OK) | Probe agregado ignorava estados por-host e auth-failed alcançável | Contagem por estado + mensagens proporcionais | test_homelab_aggregate_probe (4) |
| Chat poluído por cards técnicos (REMOTE SHELL · gateway etc.) | toolActivities renderizados inline sem filtro | Agrupador compacto por Agent Run, modo técnico OFF por padrão persistido; approvals sempre visíveis | ConversationPanel.test.ts (4) + vitest 86 ✓ |
| Bundle stale no executável Tauri real | exe embutia dist antigo (botões Proxmox "invisíveis") | Rebuild release APÓS dist atual; timestamps registrados | exe 04:11 > dist 04:0x |

Bugs documentados (não bloqueantes): `ui_click` falha COM TypeError
("parameter 2"); engine de teclado usa sintaxe `{ctrl+s}` (não `^s`);
Notepad Win11 é single-instance com abas — matching por título pode atrasar
(daily-use s04 marcado DEGRADED honesto; capacidade provada pelo E2E dedicado).

## 4. Estados oficiais de integração

| Integração | Estado na validação |
|---|---|
| Home Assistant | READY real (perfil ha-vm, Bearer obrigatório, sem HA_AUTH novos) |
| Proxmox | UNCONFIGURED honesto — aguardando API Token do operador via UI (Credential Broker) |
| OpenWrt | AUTH coerente (reachable ≠ READY sem credencial) |
| Sentinel | DISABLED intencional (não é erro) |

## 5. Testes

```text
backend full ....... 515 passed / 0 failed (253s)
frontend vitest .... 86 passed / 22 arquivos
tsc -b + vite ...... OK (dist regenerado)
cargo check/test ... OK
Tauri release ...... OK (kazumi-desktop.exe reconstruído)
daily-use .......... 15 PASS · 0 FAIL · 1 DEGRADED (notepad aba) · 2 SKIPPED
stress ............. PASS — 100 turnos 0 falhas, correlação 1.0, RAM Δ negativa
long-run ........... ver seção 7
notepad E2E ........ PASS completo (launch→PID/HWND→type→readback→save→verify→WM_CLOSE→absent)
watchdog crash ..... kill real → relaunch 56s → health PASS → servidor único
estáticos .......... diff-check ok · mojibake 0 · secret scan CLEAN
```

## 6. Lifecycle

- **Ownership**: componentes owned parados pelo lifespan com steps limitados;
  processos externos (Ollama, HA, navegador pessoal, run_backend.bat :8010)
  nunca são mortos.
- **Job Object**: os harnesses desta estação rodam destacados via Task
  Scheduler; dentro do produto o shutdown coordenado para owned pelo lifespan
  (Win32 Job Object fica para o Production Packaging — limitação registrada).
- **Single instance**: mutex do launcher + foco da instância existente.
- **Porta 8000**: livre entre sessões (restart-session espera PORT_FREE).

## 7. Long-run (P25)

```text
duração ........ 18.0 min · 165 amostras · intervalo 5s
RAM growth ..... -82,6% (warm release entre janelas)
threads ........  0,0%
handles ........ +21,4% (threshold 30%)
connections .... -33,3%
verdict ........ STABLE (leak_suspected = False)
```

## 8. Pendências externas honestas

```text
AWAITING_OPERATOR_CREDENTIAL — Proxmox API Token (inserir via Integrações › Configurar)
BLOCKED_ENVIRONMENT         — mortes nativas intermitentes de python nesta
                              estação (forrtl/console-close já conhecidas do
                              release_gate); mitigadas por watchdog + Task
                              Scheduler auto-restart + pythonw sem console
SKIPPED                     — VS Code/browser no daily-use (opt-in por design);
                              clique manual nas 13 páginas do Tauri fica para a
                              sessão do operador (bundle atual provado por:
                              vitest estrutural, timestamps dist<exe e app real aberta)
```

## 9. Veredicto final

```text
Artefato: .tmp/release-health-final.json → final_verdict = GREEN (0 hard failures)
Gates core: backend 515/0 · frontend 86/86 · vite OK · Tauri OK
            grounding runtime PASS · Notepad E2E PASS · watchdog PASS
            shutdown PASS · restart PASS · single-backend PASS
            UI smoke estrutural PASS · ghost buttons 0 · mojibake 0
            daily-use 0 FAIL (1 DEGRADED honesto) · stress PASS · long-run STABLE
Integrações opcionais: SKIPPED/UNCONFIGURED/DISABLED — não bloqueiam GREEN

FREEZE APPROVED
```

Próximas etapas exclusivas: (1) Documentação completa; (2) Production
Packaging & Windows Installer V1. Nenhuma feature nova neste ciclo.
