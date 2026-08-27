# Model Upgrade — Qwen 14B (futuro)

Guia para o upgrade futuro de modelo. **Nada abaixo é executado hoje**: o 14B
não está instalado e isso é um estado válido (`NOT INSTALLED` na UI, sem erro).

## 1. Requisitos de hardware

Não afirmar GPU específica obrigatória. Requisitos reais devem ser medidos
com a baseline do 8B como referência (`data/model-benchmarks/baselines/`):
VRAM observada, RAM idle, TTFT e tokens/s por contexto (2048/4096/8192).
O perfil `qwen-14b-candidate` já existe no Benchmark Lab e aceita qualquer
tag qwen 14B instalada — sem assumir nome exato.

## 2. Instalar o modelo

```powershell
ollama pull <tag-do-modelo-14b>
```

Nunca baixar automaticamente; nunca trocar o modelo oficial automaticamente.

## 3. Registrar no Brain Manager

Registrar a tag nas settings do Brain (sem promover): o modelo atual oficial
permanece `qwen3:8b` até promoção manual.

## 4. Executar benchmark completo

```text
POST /api/benchmark/full   {"model_id": "<tag>", "contexts": [2048,4096,8192], "repeats": 3}
POST /api/benchmark/baselines/save {"run_id": "...", "label": "..."}
```

Sem alteração de código — o lab é model-agnostic.

## 5. Comparar com o 8B

```text
POST /api/benchmark/compare {"baseline": "qwen3-8b-official", "candidate": "<label>"}
```

Tabela gerada: VRAM, RAM, TTFT, tokens/s, multi-step score, tool accuracy,
grounding score, recovery score.

## 6. Critérios de promotion (gate)

```text
tool accuracy >= current
grounding     >= current
multi-step    >  current
latency       aceitável
VRAM          estável nos contextos testados
```

A decisão é sempre manual (botão Promote na UI de benchmark). O Recommendation
Engine pode sugerir baseado em métricas, mas nunca promove sozinho.

## 7. Rollback

Brain Manager permite voltar ao `qwen3:8b` imediatamente, sem reinício do
pipeline — rollback é parte do gate, não um resgate de emergência.
