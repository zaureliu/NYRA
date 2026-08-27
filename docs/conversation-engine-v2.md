# Conversation Engine V2

## Pipeline

```text
Browser/WebView microphone
  → echo cancellation + energy turn detection
  → silence grace / max utterance
  → ConversationEngine
  → cached Faster-Whisper + Silero VAD
  → RealtimeOrchestrator
       ├─ casual: Ollama streaming
       └─ operational: Agent Loop + contextual tools
  → SentenceAssembler
  → ordered SpeechQueue
  → Kokoro pt-BR (SAPI fallback)
  → selected browser output device
```

`ConversationEngine` é a autoridade backend para os estados `IDLE`, `LISTENING`,
`USER_SPEAKING`, `TRANSCRIBING`, `THINKING`, `TOOL_EXECUTION`, `SPEAKING`,
`INTERRUPTED` e `ERROR`. Esses estados chegam ao WebSocket, dashboard, Desktop Presence
e avatar. Push-to-talk e Always Listening usam o mesmo fluxo; transcrição vazia nunca é
enviada ao LLM.

Em turnos casuais, o `ContextBuilder` omite o contexto técnico-operacional do system prompt e não fornece schemas de tools. A mensagem atual é anexada uma única vez depois de um marcador explícito de turno. Saudações isoladas não recuperam conversa curta nem memórias relevantes e usam uma resposta local determinística, evitando vazamento entre turnos ou falha por resposta vazia do modelo.

## Turn detection e interrupção

- A captura usa AEC, noise suppression e AGC quando o browser/WebView oferece suporte.
- `speech_start_ms`, pre-roll, post-roll/silence grace e duração máxima são parâmetros
  internos do pipeline, não sliders da tela normal.
- Durante playback, o VAD de barge-in exige energia e duração maiores para reduzir
  self-listening. O estado local de playback é usado imediatamente; não depende do poll.
- Barge-in chama `cancel_speech`: interrompe geração/fila/reprodução de TTS, mas não
  cancela automaticamente LLM, tool ou Agent Run.
- Cancelamento de tarefa é uma ação separada (`target=task`) e respeita a policy do agente.

## STT e TTS

Faster-Whisper é criado uma vez, faz preload em task de startup e reutiliza a mesma
instância. Os defaults V2 usam beam 1, quatro threads de CPU e um worker. O idioma é
português, mantendo termos técnicos no texto e normalizando-os somente na fala.

Kokoro ONNX com `pf_dora` é o TTS primário local. Windows SAPI (`pyttsx3`) é fallback.
Falhas geram `PRIMARY_TTS_FAILED` e `FALLBACK_TTS_USED`. A resposta é segmentada por
sentença; tokens individuais nunca são enviados ao TTS, e a fila preserva a ordem.
Chatterbox, Edge e Voice Hunter não aparecem como engines equivalentes na configuração
normal.

## Ollama Warm Manager

`OllamaWarmManager` é o único dono de preload, warm-up, keep-alive, readiness, recovery e
troca de modelo. O launcher apenas garante servidor/modelo instalado e inicia a API.

1. A API sobe sem aguardar os pesos.
2. O manager publica `OLLAMA_LOADING`.
3. O preload usa `POST /api/generate` sem prompt, messages, tools ou histórico.
4. O warm-up opcional usa `/api/chat` com o prefixo real do sistema e `num_predict=1` para
   aquecer a avaliação do primeiro turno; continua isolado, sem histórico, memória, tools,
   eventos de conversa ou TTS.
5. Readiness só vira `OLLAMA_READY` depois de `/api/ps` confirmar residência.
6. Offline/restart ou mudança do modelo dispara novo preload; o modelo anterior pode ser
   liberado conforme configuração.

O Ollama 0.32.15 deste host rejeitou `"-1"` como duration e um preload vazio com sentinela
numérico não permaneceu observável em `/api/ps`. O default validado é, portanto, `1h`.
Valores `-1` e `0` continuam aceitos na configuração e são serializados como números;
durações são enviadas como strings.

## Telemetria

Cada turno mede VAD end, STT start/complete, request Ollama, primeiro token, conclusão,
primeira sentença, request/primeiro arquivo TTS e início real de playback. O evento de
playback pode chegar depois da resposta HTTP; a telemetria conserva uma base limitada para
atualizar TTFA sem guardar texto ou áudio.

Use:

```powershell
.\.venv\Scripts\python.exe .\scripts\conversation-v2-benchmark.py
.\.venv\Scripts\python.exe .\scripts\conversation-v2-benchmark.py --unload-first --preload
```

O benchmark guarda apenas timestamps, contagens e durações. `--unload-first` é opt-in.

### Validação local de 2026-08-22

Ambiente: Ollama 0.32.15, `qwen3:8b` Q4_K_M, contexto 8192 e Kokoro `pf_dora`.

| Métrica | Antes | V2 aquecida (mediana de 3) |
| --- | ---: | ---: |
| Primeiro token visível | 3727,4 ms | 1303,4 ms |
| LLM TTFT no servidor | 3197,9 ms | 825,5 ms |
| Primeiro arquivo TTS disponível | 21306,6 ms | 2843,5 ms |
| Primeiro áudio baixado e reproduzível | não medido | 3362,8 ms |
| Resposta completa | 35399,3 ms | 3373,2 ms |
| Prompt total do turno observado | 6934 caracteres | 5398 caracteres |
| Prompt de sistema estático | 10455 caracteres | 4613 caracteres |

O teste frio direto mediu 76341,9 ms antes e 51283,0 ms depois; essa diferença inclui
variação de carregamento do host e não deve ser atribuída somente ao código. Com o
modelo residente, o `load_duration` observado ficou entre 0,9 e 1,3 ms. A recuperação
real após reiniciar o servidor Ollama levou 47761,9 ms para preload e 6800,1 ms para o
warm-up, terminando em `OLLAMA_READY` e com expiração de uma hora em `/api/ps`.

Um warm-up curto por `/api/generate` mantinha os pesos residentes, mas o primeiro prompt
conversacional ainda pagava 7012,4 ms de avaliação e tinha TTFT de 7243,3 ms. Depois de
alinhar o warm-up ao prefixo real de `/api/chat`, uma descarga controlada do modelo foi
recuperada automaticamente em 16435,7 ms. O primeiro turno subsequente mediu 1,1 ms de
load, 735,6 ms de prompt eval e TTFT de 1118,0 ms. O custo foi deslocado para a task de
startup/recovery, sem bloquear a disponibilidade HTTP da API.

O teste de TTS gera e baixa áudio real, mas não comprova som emitido pelo hardware. A
validação de STT usou um WAV real gerado localmente: `Bom dia!`, 0,896 s de áudio e
0,310 s de transcrição. Microfone, speaker físico e barge-in acústico permanecem testes
manuais dependentes do equipamento.

## Configuração

```env
NYRA_OLLAMA_PRELOAD=true
NYRA_OLLAMA_KEEP_ALIVE=1h
NYRA_OLLAMA_WARMUP=true
NYRA_OLLAMA_CONTEXT_SIZE=8192
NYRA_OLLAMA_PRELOAD_TIMEOUT_SECONDS=300
NYRA_OLLAMA_RECOVERY_INTERVAL_SECONDS=10
NYRA_OLLAMA_UNLOAD_PREVIOUS_MODEL=true

NYRA_CONVERSATION_ENGINE=true
NYRA_VOICE_BARGE_IN=true
NYRA_VOICE_STREAM_TTS=true
NYRA_STT_BEAM_SIZE=1
NYRA_STT_CPU_THREADS=4
NYRA_STT_WORKERS=1
NYRA_TTS_FALLBACK_PROVIDER=pyttsx3
```

Preferências visíveis são persistidas pelo backend em `data/settings-v33.json`.
Precedência: override explícito de código/teste, ambiente/.env, estado persistido, YAML.
Segredos não fazem parte do schema de áudio.

## Troubleshooting

- `OLLAMA_OFFLINE`: confirme o servidor em `/api/tags`; o manager tentará recuperar.
- `OLLAMA_ERROR`: consulte `nyra.ollama_warm`; readiness não foi inferida do HTTP apenas.
- STT `Loading`: o backend continua online enquanto o modelo é preparado.
- `PRIMARY_TTS_FAILED`: verifique os arquivos Kokoro; o log indicará se SAPI foi usado.
- Microfone removido: a UI volta a `default`, persiste o fallback e informa o operador.
- Eco durante barge-in: confirme AEC do WebView e evite speaker muito próximo do microfone.
