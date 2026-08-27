# NYRA Runtime Supervisor V1

Gerenciamento estruturado de processos e serviços persistentes conhecidos pela NYRA,
distinto do shell arbitrário (`system_shell` continua disponível para diagnóstico finito).

## Arquitetura

```text
User / Agent / Monitor
        ↓
Runtime Supervisor  (backend/app/runtime/supervisor.py)
        ↓
Service Registry    (config/runtime_services.yaml)
        ↓
Process Manager     (spawn/stop com identidade psutil; OWNED PROCESS)
        ↓
Health / Readiness  (PROCESS | TCP | HTTP | COMMAND | WARM_MANAGER | SENTINEL)
        ↓
EventBus (runtime.*) → UI / Desktop Presence
        ↓
Structured Result (grounding: execution_success / effect_verified / verification_status)
```

## Componentes

| Arquivo | Finalidade |
| --- | --- |
| `backend/app/runtime/models.py` | Estados normalizados (`UNKNOWN…CRASH_LOOP`), `ServiceSpec`, snapshots, `operation_result` com grounding fields |
| `backend/app/runtime/registry.py` | Carga/validação do YAML (IDs duplicados, dependência inexistente, ciclos, health inválido, paths ausentes). Entrada inválida vira `INVALID_CONFIGURATION` sem derrubar as demais |
| `backend/app/runtime/process_manager.py` | Spawn detached Windows (DETACHED+NEW_GROUP+NO_WINDOW), stdout/stderr em log rotativo, identidade por PID+create_time+cmdline fingerprint, parada graciosa com fallback controlado `/T /F`, rotação de log |
| `backend/app/runtime/health.py` | Checks estruturados + hooks externos (Warm Manager, Sentinel connector) |
| `backend/app/runtime/logs.py` | Tail com redaction, truncamento e limite de caracteres |
| `backend/app/runtime/history.py` | SQLite `runtime_events`: timestamp/service/action/previous_state/new_state/duration/success/error_code/agent_run_id/approval_id |
| `backend/app/runtime/supervisor.py` | Ciclo de vida completo, idempotência, locks por serviço, crash-loop/backoff, auto-recovery, monitor, shutdown policy |
| `backend/app/runtime/tools.py` | Tools nativas com risco por ação e ApprovalGate vinculado |

## Serviços registrados (reais)

| id | type | ownership | start mechanism | health | readiness | capabilities | auto-recovery |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `nyra_backend` | PROCESS | OWNED | `python -m uvicorn app.main:app --port 8000` | HTTP `/api/health` | HEALTH_PASS | status/health/start/logs (stop/restart **desabilitados**: self-restart exige supervisor externo) | off |
| `nyra_frontend_dev` | PROCESS | OWNED | `node vite.js` (dev; produção via Tauri não depende disso) | TCP 5173 | HEALTH_PASS | todas | off |
| `ollama` | EXTERNAL_SERVICE | EXTERNAL | instalação própria (nunca iniciar segunda instância) | WARM_MANAGER hook | OLLAMA_WARM (critério do Warm Manager existente) | status/health | off |
| `utamo_sentinel` | EXTERNAL_SERVICE | EXTERNAL | bridge Sentinel existente | SENTINEL hook | HEALTH_PASS | status/health | off |
| `nyra_test_service` | PROCESS | OWNED | `scripts/runtime_test_service.py` (HTTP 18765/health) | HTTP | HEALTH_PASS | todas | off |

Tokens do registry: `{python}` e `{repo_root}`.

## Tools nativas

`runtime_status`, `runtime_health`, `runtime_logs` (READ_ONLY), `runtime_start`
(risco por spec, default LOW_RISK), `runtime_stop`/`runtime_restart` (ELEVATED).
Sem approval_id, mutações ELEVATED retornam `APPROVAL_REQUIRED` + `approval_id`
emitido pelo mesmo `ShellApprovalGate` do shell (fingerprint vinculado a
ação+serviço+agent_run); IDs são single-use e intransferíveis entre ações.
Service ID só resolve se existir no registry (nada de `service="python evil.py"`),
e nenhuma tool aceita command arbitrário.

## Fluxos garantidos

```text
START   : estado → idempotência (READY/RUNNING ⇒ already_running) → deps → spawn
          → wait startup_timeout com retries → health/readiness → READY verificado
STOP    : identidade válida → gracioso → aguardar → fallback controlado → ausência verificada
RESTART : STATUS → STOP → VERIFY STOPPED → START → VERIFY READY
CRASH   : falhas com processo morto marcadas na janela (max_restarts/600s padrão)
          ⇒ CRASH_LOOP bloqueia novas tentativas até expirar; backoff 2s/5s/10s na auto-recovery
LOCKS   : uma mutação por serviço; segunda espera (lock_wait_seconds=10) ou OPERATION_LOCKED;
          liberação garantida em sucesso/falha/cancel (finally)
SHUTDOWN: nunca encerra EXTERNAL; OWNED segue shutdown_policy (default LEAVE_RUNNING)
```

## API (prefixo `/api`)

```text
GET  /runtime/services                     lista com estados reais
GET  /runtime/services/{id}                detalhe/inspeção
GET  /runtime/services/{id}/health         check agora
GET  /runtime/services/{id}/logs?lines=N   tail redacted (default 100, máx chars limitado)
POST /runtime/services/{id}/start|stop|restart  body {"approval_id"?: str}
GET  /runtime/history                      metadados das operações
POST /shell/approvals/{approval_id}        decisão de approval (fluxo existente)
```

## Agent Loop

Tools registradas no ToolRegistry com preflight de risco e `resource_key=runtime:<id>`
(integra com locks do Agent Controller). System prompt instrui preferência por
`runtime_*` sobre `system_shell` para serviços cadastrados. Resultados estruturados
alimentam o grounding (nenhum valor inventado; VERIFIED só com check real).

## Self-restart (limitação declarada)

Reiniciar o próprio backend exige mecanismo externo (watchdog/launcher/service).
Não existe ainda — `runtime_restart(nyra_backend)` retorna
`SELF_RESTART_UNSUPPORTED` em vez de fingir. Start pós-crash permanece possível
(spawn de nova instância quando nada está escutando).

## Configuração (Settings, env prefix NYRA_)

```text
RUNTIME_SUPERVISOR_ENABLED=true
RUNTIME_SERVICES_PATH=config/runtime_services.yaml
RUNTIME_HEALTH_INTERVAL_SECONDS=15      RUNTIME_DEFAULT_STARTUP_TIMEOUT_SECONDS=30
RUNTIME_MAX_RESTARTS=3                  RUNTIME_RESTART_WINDOW_SECONDS=600
RUNTIME_LOG_TAIL_LINES=100              RUNTIME_LOG_MAX_CHARS=50000
RUNTIME_ALERT_COOLDOWN_SECONDS=120      RUNTIME_AUTO_RECOVERY_SERVICES=""  # vazio = nenhuma
```

## Testes

```bash
.venv\Scripts\python.exe -m pytest backend/tests/test_runtime_supervisor.py -q   # 28 testes
& .venv\Scripts\python.exe scripts\runtime-supervisor-smoke.py                   # smoke real
```

Cobertura: registry válido/inválido/duplicado/disabled/path/health/dep/ciclo;
lifecycle start/idempotência/stop/restart; timeout com processo vivo (DEGRADED,
sem marca de crash); crash imediato (FAILED+marca); proteção CRASH_LOOP;
serialização e rejeição por lock; logs redaction/truncation/inexistente;
approval obrigatória/mismatch/replay/serviço desconhecido; agente usando
runtime_* e respeitando WAITING_APPROVAL; allowlist de auto-recovery; hook do
Warm Manager gate de readiness; settings defaults.
