# Benchmark V4 em tempo real

Data: 2026-08-19. Host Windows atual, Ollama `qwen3:8b`, faster-whisper existente e Edge TTS. Números são medições reais de uma execução, não garantias; rede, cache e carga alteram TTS/LLM.

## Antes e depois

| Cenário | Primeiro token | Primeira sentença | Primeiro áudio | Completo | Chunks |
|---|---:|---:|---:|---:|---:|
| Baseline V3, curta e sequencial | — | — | 3126 ms | 3126 ms | 1 |
| V4 streaming, curta | 1213 ms | 1314 ms | 3063 ms | 3122 ms | 1 |
| V4 streaming, 6 sentenças | 2919 ms | 3597 ms | 5691 ms | 16358 ms | 6 |
| V4 sem sentence streaming, mesmas 6 sentenças | 4526 ms | 4526 ms | 6156 ms | 13807 ms | 6 |
| V4 streaming, 8 sentenças técnicas | 3412 ms | 5061 ms | 8914 ms | 29236 ms | 8 |

No caso de seis sentenças, a fala começou cerca de 465 ms antes do modo sem streaming e 10,7 s antes do fim da resposta streamada. No teste técnico, iniciou abaixo da meta de experiência de 12 s. A métrica interna `end-to-first-audio` da resposta curta foi 2769 ms.

Um primeiro teste cold ficou sem primeiro token por mais de 60 s e foi cancelado. Uma chamada Ollama imediatamente posterior respondeu em aproximadamente 1096 ms. O resultado é registrado como outlier real; warmup reduz probabilidade, mas não transforma rede/model load em garantia.

Após o reinício total final, o carregamento explícito do modelo levou 15,56 s (10,39 s somente de load). A conversa curta seguinte iniciou áudio em 9,62 s, com primeiro token em 8,02 s e TTS em 1,49 s. Continua abaixo da meta de 12 s quando o modelo já está residente, mas confirma que o principal gargalo variável deste host é o Ollama, não a fila de áudio.

STT observado nos logs do microfone ficou aproximadamente entre 280 e 340 ms para capturas recentes. Interrupção real durante resposta longa, disparada após 900 ms, cancelou a requisição, zerou a fila e retornou para `LISTENING`.

## Recursos

| Estado | CPU sistema | RAM sistema | RSS backend | RSS frontend |
|---|---:|---:|---:|---:|
| Idle amostrado | 15,0% | 70,6% | 123,7 MB | 98,8 MB |
| Conversa amostrada | 15,1% | 70,6% | 125,6 MB | 98,8 MB |

Ollama aquecido e VTube Studio estavam presentes durante a amostra de RAM do sistema. A espera de rede do Edge fez a amostra pontual de CPU do processo ficar próxima de zero; use o painel para séries representativas.

## Eventos

Um evento debug de recuperação do Network Watch chegou à reação visual em 122,1 ms. O teste real do Sentinel passou no checkpoint; depois, o servidor externo configurado em `127.0.0.1:5000` ficou offline. A configuração do usuário não foi alterada e a integração permanece coberta por testes/mocks.

## V5 Brain

Com `qwen3:8b` residente, a rodada final mediu primeiro token externo em 1,60 s e primeiro áudio em 4,08 s (server end-to-first-audio 3,80 s). Trocas entre 8B/9B causaram cold loads entre 146 e 174 s no armazenamento/hardware atual; por isso o Brain Lab não troca automaticamente o oficial e o warmup é explícito.

Na validação posterior ao reinício completo da V5, a primeira fala chegou ao áudio em 9,27 s (first token 4,62 s) e a repetição aquecida em 4,74 s (first token 2,21 s). A variação confirma que residência/cache do Ollama deve ser considerada na experiência real.
