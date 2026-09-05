# Model Benchmark Lab

Implementação das Partes K-Q e AZ/BA do prompt10 (`backend/app/benchmark/lab.py`,
UI em `frontend/src/components/BenchmarkPanel.tsx`).

## Princípios

- Reproduzível e isolado: benchmark **nunca** altera o modelo oficial nem baixa
  modelos automaticamente.
- Modelo ausente é estado válido: `MODEL_NOT_INSTALLED` (UI mostra
  "NOT INSTALLED"), sem erro.
- Runs rodam em background (Job-style) para nunca travar o chat.
- Scoring determinístico — o mesmo LLM nunca é o único juiz.

## Métricas de performance

Por contexto (2048/4096/8192) e repetição (default 3, mínimo recomendado):

| Métrica | Origem |
| --- | --- |
| cold load | unload controlado + load |
| warm load | Warm Manager residente |
| TTFT | tempo até primeiro token |
| tokens/s | eval_count / eval_duration |
| prompt_eval / eval_duration / total_duration | API do Ollama |
| RAM / VRAM | medição do host |

Sempre reportar **mediana**; p95 somente com amostra suficiente (≥4).

## Quality benchmark (tarefas reais da KAZUMI)

Categorias com verificação determinística: Conversation, Tool Selection,
Multi-step, Troubleshooting, Recovery, Grounding, Turn Isolation, Homelab,
Browser, Workflow. Scores: tool accuracy (tool esperada chamada), completion
(verificação real da tarefa), hallucination (fatos inventados), retries,
tool calls, latência, failure recovery (sucesso pós-falha simulada).

## API

```text
GET  /api/benchmark/profiles            perfis instalados/candidatos
POST /api/benchmark/perf                {model_id, contexts?, repeats?}
POST /api/benchmark/quality             {model_id}
POST /api/benchmark/full                perf + quality
GET  /api/benchmark/runs                lista runs
GET  /api/benchmark/runs/{run_id}       detalhe/progresso
POST /api/benchmark/baselines/save      {run_id, label} — salva oficial
GET  /api/benchmark/baselines           baselines salvos
POST /api/benchmark/compare             {baseline, candidate}
```

Baselines ficam em `data/model-benchmarks/baselines/*.json`. A baseline oficial
atual do `qwen3:8b` é a referência; thresholds de regressão (TTFT +50%,
RAM +30%, configuráveis) geram warning na comparação.

## Perfis futuros

`qwen-14b-candidate` é um perfil genérico (regex aceita qualquer tag qwen 14B).
Quando o modelo for instalado, o benchmark executa sem mudança de código.
Promotion é sempre manual — o usuário decide; ver `model-upgrade-14b.md`.
