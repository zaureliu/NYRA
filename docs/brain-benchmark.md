# Brain Benchmark V5

O benchmark A/B usa o mesmo system prompt, temperatura 0,65, top-p 0,9, thinking OFF e contexto 8192. Casos: persona, Sentinel, Network, humor seco e explicação técnica. Registra first token, total, tokens, tokens/s e resposta para avaliação humana.

Pontuações automáticas são apenas triagem. Tool use é validado pela execução do registry, e memória por memória temporária removível; não se atribui nota perfeita por regex.

## Resultado real

| Métrica textual (5 casos) | qwen3:8b | qwen3.5:9b |
|---|---:|---:|
| First token médio | 580 ms | 35.192 ms com cold |
| First token aquecido | 536–637 ms | 2.751–3.008 ms |
| Total médio | 4.128 ms | 38.828 ms com cold |
| Tokens/s | 21,16 | 25,80 |
| Cold load observado | — | 164.479 ms |

O 9B tem throughput 22% maior depois do primeiro token, mas first-token aquecido cerca de 4,8× pior. Também foi mais verboso e atribuiu perda/latência a cabo, aquecimento ou hardware sem evidência. O 8B foi mais curto, embora também tenha listado hipóteses no alerta Sentinel.

Validações funcionais do 8B: memória temporária `Orion` recuperada corretamente e removida; `Kazumi, verifica como está minha conexão` executou a skill allowlisted `network_status`. Score técnico humano aproximado:

| Critério | 8B | 9B |
|---|---:|---:|
| Conversation quality | 8,0 | 7,5 |
| Persona adherence | 7,5 | 6,5 |
| Tool use | 9,0 | não repetido após contenção de residência |
| Context understanding | 8,0 | 7,0 |
| Sentinel reasoning | 6,5 | 6,0 |
| Portuguese | 9,0 | 9,0 |
| Conciseness | 8,5 | 5,5 |
| Overall realtime suitability | **9,0** | **5,0** |

Uma troca 9B→8B fez o primeiro áudio do 8B levar 174,5 s; outro carregamento frio do 8B levou 146,7 s. Aquecido e residente, o 8B iniciou áudio em 4,08 s. Recomendação: manter `qwen3:8b` oficial; `qwen3.5:9b` fica instalado como candidato de raciocínio/manual. Nenhuma seleção automática foi feita.
