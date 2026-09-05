# Workflow Engine

Engine estruturada e confiável (`backend/app/operator/workflows.py`, spec Partes
H-J e BB). Workflow NÃO é prompt gigante — é procedimento executável.

## Step model

Todo step possui:

```text
step_id / tool / arguments(params) / dependencies / risk /
verification probe / retry_policy / rollback / timeout / status
```

Statuses: PENDING, RUNNING, SUCCEEDED, VERIFIED, FAILED, SKIPPED,
WAITING_FOR_USER, ROLLED_BACK.
Run states: RUNNING, SUCCEEDED, FAILED, CANCELLED, WAITING_FOR_USER.

## Garantias

- **Dependency graph** com validação de ciclos antes de iniciar.
- **Output binding**: `{step_id.output.field.sub}` como input de step posterior.
- **Parameter validation** antes da execução (falha rápida, sem execução parcial).
- **Preflight**: tools disponíveis? recursos? credenciais? approvals esperadas?
- **Dry run**: mostra o plano sem executar nada.
- **Resume**: steps já VERIFIED/SUCCEEDED não repetem após interrupção (§53-54).
- **Rollback por step** quando declarado (§55).
- **Retry por step**, limitado (0-3), só falhas transitórias, backoff
  configurável (§56).
- **WAITING_FOR_USER**: resultados que exigem approval pausam o run (§57);
  nenhum texto de LLM aprova nada — approval é de uso único, vinculado.
- **História persistida** (`data/workflows.json`): started, steps, results,
  verification, finish (§58).
- **Resource lock por workflow_id**: sem double-run simultâneo.
- Approvals e grounding continuam valendo na execução: todo step é uma chamada
  normal ao ToolRegistry (risk, timeout, redaction, auditoria).

## Templates reais

`config/workflow_templates.json` — mínimo: Open Development Environment,
Check KAZUMI Health, Check Homelab, Open Application and File, Build Project,
Diagnose Local Service, Diagnose Remote Host. Templates usam tools
estruturadas, nunca shell raw quando existe capability dedicada (§62).

Aprendizado: repetição de sequência pode gerar **sugestão** de workflow;
criação exige intenção explícita do usuário e nome natural/editável (§63-65).

## UI (Parte BB)

Workflows listados com name, steps, risk, last run, status; botões Editar
(validado), Dry Run, Run, Cancel e History.

## Testes

`backend/tests/test_operator_tasks_workflows.py`,
`test_production_hardening.py` (resume sem repetir step, ciclo, binding,
dry-run, retry transitório, WAITING_FOR_USER).
