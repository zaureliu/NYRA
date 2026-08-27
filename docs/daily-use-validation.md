# Daily-Use Validation

Validação com runtime real (spec Partes S-AH, BI, BH, BC-BD). Nada de
"funciona só no smoke test": toda etapa real exige prova verificável.

## Suíte E2E diária — `scripts/daily_use_e2e.py`

Executa contra o backend real (:8000) + Ollama os cenários:

```text
hello → follow-up → isolamento de turno → Notepad (abre/digita/salva/fecha,
arquivo verificado no filesystem) → browser → filesystem em temp
(mkdir/write/read/rename/copy/delete) → shell (echo / Get-Date) →
runtime supervisor → Home Assistant (API real) → homelab overview →
OpenWrt (auth failure ≠ offline) → persistent job (chat continua responsivo)
→ workflow Check NYRA Health → recovery controlado → watchdog (harness seguro)
→ voice (quando hardware disponível) → hello final SEM vazamento
```

Categorias por cenário: `PASS / DEGRADED / FAIL / SKIPPED`.
Integrações opcionais ausentes são SKIPPED/DEGRADED honestos — nunca FAIL
falso nem sucesso inventado (§249, §292). Provas: janela real, arquivo real,
API 200, exit code do job, health pós-recovery. O hello final não pode conter
tokens dos cenários anteriores (anti-leak).

Relatório: `.tmp/daily-use-report.json`.

## Daily Check mode — `backend/app/core/daily_check.py`

Comando interno acionável por API/UI, **nunca automático** por padrão.
Executa somente checks safe/read-only + fixtures controladas em temp, uma
categoria por vez com isolamento total:

```text
Conversation / LLM / Voice / Desktop / Browser / Filesystem /
Runtime / Jobs / Workflows / Watchdog / Homelab / Integrations
```

Histórico persistido em `data/daily-check-history.jsonl` para comparação
temporal e detecção de regressão.

## Stress — `scripts/stress_daily.py`

100 turnos simples sequenciais sem leak; 25 tool calls read-only com
correlação payload↔resposta 1:1; injeção de eventos atrasados descartada;
inputs concorrentes conforme policy; RAM/handles/threads do backend medidos
antes/depois. Relatório: `.tmp/stress-daily-report.json`.

## Long-run — `scripts/long_run_harness.py`

Backend ativo por período prolongado com monitoramento de threads, tasks,
handles, memória e processos. Quando o tempo de execução não permite horas
reais, harness acelerado documenta a limitação.

## Regression gate

`scripts/release_gate.py` agrega backend tests, frontend tests, build,
diff-check, daily E2E e stress no veredito GREEN/YELLOW/RED (ver
`production-hardening.md`). Build não é saudável se qualquer gate obrigatório
falha — mesmo com unitários passando.
