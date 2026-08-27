# Production Hardening V1

Consolidação de confiabilidade da NYRA (spec `prompt10` Partes C-G, AL-AR, AW-AX, AY).
Objetivo: as features que existem funcionam repetidamente, são mensuradas,
recuperáveis e passam testes reais — não "muitas features".

## Matriz de saúde de subsistemas

`backend/app/core/health_matrix.py` normaliza todos os subsistemas no vocabulário:

```text
DISABLED / UNCONFIGURED / STARTING / READY / DEGRADED / FAILED / OFFLINE / RECOVERING / STALE
```

- READY significa READY: considera dependências reais (ex.: processo Ollama +
  API + modelo carregado = `OLLAMA_READY`, nunca "processo existe = pronto").
- Todo estado carrega timestamp (`observed_at`); estados sem refresh viram
  `STALE` após `STALE_AFTER_SECONDS` (180 s por padrão).
- Falha de integração opcional (Sentinel, Proxmox, Home Assistant) nunca derruba
  chat, voz, local operator ou o próprio relatório (failure isolation).
- `health_report` consolidado inclui core, LLM, voice, desktop, jobs, watchdog,
  homelab, integrations e database.

## Concorrência e isolamento

- Turn isolation (`app/core/turn.py`): um turno ativo por vez, eventos atrasados
  descartados via contador `late_events_dropped`, zero turnos órfãos.
- Task/Job/Workflow isolation: nenhuma execução contamina outra; locks por
  recurso com timeout, owner e release em `finally` (deadlock protection).
- Double-click protection na UI + lock por `workflow_id` no Workflow Engine.

## State consistency

Backend é a única autoridade de estado operacional; frontend apenas reflete.
Todo estado operacional tem timestamp e endpoints de current-state forçam
refresh quando o cache está stale.

## Tool hardening

- Inventário completo em `backend/app/operator/tools_reg.py`: tool, risk,
  side_effect, verification, timeout, resource_lock, grounding.
- Toda mutation exige verification policy; tool sem verifier retorna
  `NOT_VERIFIED`.
- Timeout obrigatório em toda tool externa; retry só para falhas transitórias,
  limitado e configurável, com backoff quando apropriado.
- Shell local só via `system_shell` (classificação READ_ONLY → CRITICAL);
  SSH só via `remote_shell` para hosts do Trusted Host Registry.

## Agent hardening

- Limites: max steps (12), max tool calls (20), max runtime (300 s), detecção
  de comando repetido e de failures consecutivos.
- Resposta vazia do Ollama: 1 retry controlado; segunda falha é honesta.
- Duplicate tool call detection: mesma ação + mesmo resultado ⇒ reavaliar.
- Sem chain-of-thought persistido; fatos/inferências/suposições separados.

## Startup / shutdown / crash recovery

- Cold boot testado sem processos duplicados; preload/warmup do Ollama medidos
  (métricas em `/health`).
- Graceful shutdown encerra serviços owned conforme policy e nunca mata serviços
  externos; jobs persistem e fazem reconcile no retorno.
- Watchdog detecta crash do backend, relança e reconcilia estado
  (`watchdog/`, `backend/app/core/watchdog*`; harness seguro nos testes).

## Database / config / secrets

- SQLite: verificação de integridade, migrations testadas, corrupção nunca é
  "consertada" destruindo o banco; backup mínimo em `data/recovery-backups/`.
- Config validada no startup; setting inválido não derruba o processo; secret
  ausente marca a integração como `UNCONFIGURED`.
- `.env` carregado independente do CWD (testado a partir de repo root,
  `backend/` e script).
- Redaction aplicada em logs/exceções; scan de payloads do frontend sem secrets.

## UI / Error UX

- Frontend recebe sempre `error_code`, `safe_message`, `stage`, `recoverable`
  (`app/core/errors.py`) — nunca stack trace.
- Backend offline ou stale aparece como offline/stale na UI; nenhum status fake,
  nenhum mojibake (auditoria `app/core/encoding_audit.py`).

## Verificação

- `backend/tests/test_production_hardening.py` cobre os contratos acima.
- `scripts/release_gate.py` agrega tudo no veredito GREEN/YELLOW/RED.
