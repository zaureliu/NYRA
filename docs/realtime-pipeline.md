# Pipeline de tempo real V4

A V4 adiciona `RealtimeOrchestrator` sobre os providers existentes. O caminho falado é progressivo:

```text
STT final -> LLMProvider.stream -> SentenceAssembler -> Pronunciation Engine
          -> TTS -> Voice Processor -> SpeechQueue -> WebSocket/audio -> avatar
```

Cada resposta recebe um `response_id`. O assembler protege abreviações, versões, IPs e URLs, aplica limites mínimos de palavras/caracteres e usa timeout para não deixar um fragmento natural preso. Sínteses podem terminar em paralelo, mas `SpeechQueue` e o cliente sempre entregam os índices em ordem.

## Cancelamento e duplex

`HALF_DUPLEX` é o padrão seguro: a captura é suspensa enquanto a KAZUMI fala. `SMART_DUPLEX` habilita barge-in somente quando também autorizado nas configurações. `USER_SPEECH_STARTED` cancela o stream, invalida chunks pendentes, interrompe a reprodução e devolve o estado para `LISTENING`.

A proteção contra autoescuta combina estado de fala, guard interval, fila conhecida de áudio, informações do dispositivo, `echoCancellation` e VAD. O modo inteligente é experimental porque cancelamento de eco depende do dispositivo/driver.

## Telemetria

O painel `Settings > Developer > Realtime` mostra status, fila, atenção, percepção, reação, avatar e uma timeline curta. São registrados timestamps de desempenho, não o conteúdo integral da conversa:

- fim da fala/STT final;
- início do stream e primeiro token;
- primeira sentença;
- início do TTS e primeiro áudio;
- conclusão ou interrupção.

O provider Ollama continua usando `qwen3:8b` e agora consome o NDJSON de streaming nativo. Nenhum modelo foi trocado.
