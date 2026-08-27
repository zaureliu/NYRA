# NYRA — Production Hardening V1 — Relatório Final

Data: 2025-08-23 · Branch `feature/nyra-avatar-v2` · Modelo oficial: **qwen3:8b** (inalterado)
Veredito de release: **YELLOW** — nenhum gate obrigatório falhou; 1 warning honesto.

## 1. Estado inicial e preservação

- Sem `reset/clean/restore` destrutivo; nada foi revertido.
- Trabalho reconstruído a partir de git status/diff, mtimes e artefatos existentes
  (`health_matrix`, `benchmark/lab.py`, workflows, daily_check já implementados).

## 2. Hardening — bugs encontrados e corrigidos nesta sessão

| # | Bug | Correção |
|---|-----|----------|
| 1 | Benchmark Lab gravava baselines em `backend/data` (path divergente do release gate) | `lab.py` usa `DATA_ROOT` (§27 single source of truth) |
| 2 | `release_gate.py` apontava REPO_ROOT para `scripts/`; crash no 1º passo | `parents[1]` + stdout UTF-8 seguro (UnicodeEncodeError em cp1252) |
| 3 | Turno substituído vazava `CancelledError` → HTTP 500 e turno órfão no registro | `converse` converte em `PipelineFailure(TURN_SUPERSEDED)` → **409**; try cobre todo o turno (inclusive telemetry/publish) |
| 4 | Approval de desktop/fs: `risk_level.value` em string crua → 500 na decisão | normalização em `decide_approval`/`resolve_user_approval`/`public_dict` |
| 5 | Fingerprint de approval divergente entre request/consume (fluxo two-phase nunca funcionou para fs/desktop) | gate aceita fingerprint pré-computado; controller repassa o mesmo tuple |
| 6 | `FsPathInput` sem campo `approval_id` → schema descartava a chave | adicionado ao schema |
| 7 | Browser operava sobre aba órfã/página oculta: cliques descartados pelo Chrome 151 com status VERIFIED falso | `resolve_tab` prefere páginas http(s); guard `PAGE_NOT_VISIBLE` + ativação de aba antes de input (§12/§259) |
| 8 | Teste browser sem isolamento (poluição de aba de sessão anterior) | navegação explícita no teste (§21) |
| 9 | `stress_daily`: PID errado (pegava listener não-uvicorn), sleep fixo deixava turnos ativos, 409 contado como falha | filtro uvicorn no cmdline, settle por polling, 409=política conforme |
| 10 | `daily_use_e2e`: payload plano (API exige `{"parameters":...}`), resultado de cenário sempre FAIL (duplo finish), job_id/logs aninhados, rotas homelab erradas, campos `app_id` inexistentes | todos corrigidos + fluxo de approval legítimo em duas fases |
| 11 | `long_run_harness`: `DictWriter.flush()` inexistente | `handle.flush()` |

## 3. Concorrência (Parte D/AJ)

3 inputs simultâneos ⇒ **1×200 + 2×409 TURN_SUPERSEDED**, `active_turns_after_settle: 0`
(antes: 2×500 + turnos órfãos). Late events monotônicos; correlação tool-call 1.0.

## 4. Tool audit (§300)

Total **141**: READ_ONLY 75 · LOW_RISK 37 · DYNAMIC 16 · ELEVATED 9 · DESTRUCTIVE 4.
Mutations exigem verification/approval; sem verifier ⇒ NOT_VERIFIED; timeouts obrigatórios.

## 5. Daily-use E2E real (§302)

15 PASS · 1 DEGRADED · 0 FAIL · 2 SKIPPED (honestos).
DEGRADED: digitação no Notepad indisponível neste ambiente (arquivo real provado;
janela não enumerada) — limitação conhecida, não mascarada (§292).

## 6. Benchmark qwen3:8b (§303-§304) — baseline `qwen3-8b-official`

VRAM carregada ~6.19 GB · RAM host 17.76 GB (69.4%) · contexto observado 8192.

| Métrica | cold | warm mediana |
|---|---|---|
| load_ms | 9 207 (mediana 3 contextos) | 1.1–22 s* |
| TTFT | 9.3–10.1 s | 232 ms–19.7 s* |
| tokens/s | 72–80 | 71–79 |
| prompt_eval (43 tok) | 162–193 ms | 31–199 ms |

\* primeiras cargas pós-unload recarregam pesos (~20 s); regime residente: TTFT ~230–420 ms.

Quality (14 casos, scoring determinístico): **13 passed**. Falha: `grounding_empty_result`
(modelo tende a preencher resposta quando o resultado vem vazio — item de refinamento,
não bloqueia release).

## 7. Future 14B (§305-§307)

installed: **NO** · benchmark-ready: **YES** (`qwen-14b-candidate` genérico; ausência
retorna NOT INSTALLED sem erro; comparação via `/api/benchmark/compare`; promoção manual).

## 8. Performance (§308)

Backend idle RSS ~455–520 MB; stress RAM +15.3 MB / threads ±0; long-run 2 min acelerado
**STABLE** (RAM 0.0%, threads −3.2%, handles −0.8%) — limitação documentada no relatório JSON.
TTFT quente ~230–420 ms; TTS ~2.9 s; startup com preload/warmup medidos em /health.

## 9. Testes (§315)

backend pytest **405 passed** · frontend vitest 18 arquivos/59 testes **passed** ·
tsc+vite build **OK** · `git diff --check` limpo · daily E2E **0 FAIL** ·
stress **PASS** · long-run **STABLE**.

## 10. Release Health (§316): YELLOW

Justificativa: todos os gates obrigatórios verdes; warning único = cenário DEGRADED
honesto (digitação notepad) + integrações opcionais SKIPPED/UNCONFIGURED (Proxmox auth,
OpenWrt SSH, Sentinel offline, VS Code/browser desabilitados por segurança do operador).

## 11. Limitações conhecidas (§317)

1. Digitação UI automatizada indisponível nesta sessão (cenário 04 DEGRADED).
2. Grounding para resultados vazios no qwen3:8b (13/14 no quality bench).
3. Long-run é harness acelerado (2 min); evidência de horas reais requer execução prolongada.
4. Watchdog validado em modo passivo/harness; reinício real fica para sessão com operador.
5. Watchdog externo relançava backend com Python do sistema (duplicação venv/base);
   processo parado durante os testes — revisar launcher antes de reativar.

## 12. Artefatos

`.tmp/release-health.json` · `.tmp/daily-use-report.json` · `.tmp/stress-daily-report.json`
· `.tmp/long-run-report.json` · `data/model-benchmarks/baselines/qwen3-8b-official.json`
· docs novos: production-hardening, model-benchmark, workflow-engine,
daily-use-validation, model-upgrade-14b, este relatório.
