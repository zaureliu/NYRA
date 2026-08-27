# Ollama Brain V5

`BrainManager` mantém providers abstratos e oferece seleção runtime entre `qwen3:8b` e `qwen3.5:9b`. O oficial é persistido apenas após confirmação em `data/brain-settings.json`; o 8B permanece fallback e nunca é removido.

Política: `think=false` para conversa realtime/alertas e contexto 8192. O Brain Lab pode usar um modelo temporariamente, restaurar o oficial e executar o mesmo prompt/system/context nos dois. Em falha antes de qualquer token, fallback automático usa o 8B; após tokens emitidos não mistura respostas.

Somente o cérebro selecionado deve permanecer residente em produção. O benchmark alterna modelos deliberadamente e portanto inclui custos de carregamento.
