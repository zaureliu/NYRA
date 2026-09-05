# Speech Recognition Providers V1

## Inventário e ownership

Baseline canônico: `896ef8ad63f2cb3f29811e555cffdf1990e0cdc4`, em `<REPO_ROOT>`.
Checkpoint local: `checkpoint/pre-deepgram-stt-20260904-896ef8a`. Nenhum reset,
worktree de implementação ou push é necessário.

O runtime oficial abre `KAZUMI.lnk` → `scripts/launch-kazumi.vbs` → launcher →
Tauri release e seu backend PyInstaller. **Desktop Presence/WebView** captura o
microfone através de `getUserMedia`, AEC/NS/AGC do navegador e AudioContext mono
a 48 kHz. Não foi encontrado o diretório informado
`C:\Users\<USER>\Desktop\Kazumi-Voice-Satellite` neste host. `backend/app/voice/`
também não existe neste baseline. O VoiceProcessorBridge existente é uma sonda
de health/capabilities no loopback; não é o proprietário desta captura.

Antes: `useAlwaysListening` acumulava WAV PCM; `usePushToTalk` acumulava WebM/
Opus. POST `/listening/utterance` ou `/conversation/turn` chegava ao
ConversationEngine → FasterWhisperSTT (`tiny` na configuração encontrada).
Silero ONNX validava o áudio, decodificado a 16 kHz no backend.
SpeechQueue gerencia saída TTS, não captura nem STT.

Depois:

```text
mesmo getUserMedia / AudioContext / enquadramento local por energia
  → frames PCM16 mono a 48 kHz (4096 amostras, cerca de 85 ms)
  → bridge Tauri ou WebSocket local autenticado
  → STTProviderRegistry / RecognitionSession
      → DeepgramSTTProvider (streaming real)
      → FasterWhisperSTTProvider (buffer, Silero e modelo existentes)
  → CanonicalTranscript final montado
  → ConversationEngine / decisão wake-word existente / converse
  → caminho oficial de texto, memória e resposta
```

Não há captura nativa, segundo loop WASAPI, segundo Silero, nem outro Satellite.
Web Locks e mensagens sem áudio em BroadcastChannel coordenam a captura manual
e a escuta nas janelas existentes. O bridge Rust é transporte de bytes: não
possui provider, modelo, credencial nem lógica de conversação.

## Configuração e credencial

Em Voice/Audio → **Speech Recognition**, selecionar Deepgram informa Cloud STT;
Faster-Whisper informa Local STT. A instalação permanece local-first até seleção
explícita. “Salvar no Broker e selecionar Deepgram” armazena a chave e seleciona
Deepgram. Só selecionar sem chave mantém o fallback local.

O identificador é `deepgram_api_key`, exclusivamente no Credential Broker
(Windows Credential Manager / cofre protegido por DPAPI). O backend resolve o
segredo somente para montar o header Authorization da conexão oficial. Não há
leitura de chave pela UI, retorno parcial de chave, `.env`, query, armazenamento
em navegador ou arquivo plaintext. A entrada password é transitória e apagada
ao salvar. Erros de validação da credencial são sanitizados sem eco do request.
O logger privado do transporte remoto desabilita logs de handshake até em DEBUG.

Preferências não secretas persistem em `settings-v33.json`, campo
`stt_recognition`, através do mecanismo de runtime existente. Defaults:

| Opção | Valor |
| --- | --- |
| Modelo / idioma | nova-3 / pt-BR |
| Smart Format / Interim Results | true / true |
| Endpointing / Utterance End | 300 / 1000 ms |
| VAD events / punctuate / numerals | true / true / true |
| Profanity filter / diarize / redact / dictation | false |
| Fallback | faster_whisper |
| Keyterms | off; até 20 termos específicos |

Advanced expõe endpointing (100–2000 ms), utterance end (1000–5000 ms),
diarização e keyterms. O formato real vem da captura: linear16/mono/sample rate
informado pelo AudioContext. Deepgram não sofre resampling; apenas o fallback
reutiliza o decoder existente a 16 kHz. `get_dynamic_keyterms(context)` é um
extension point vazio, sem enviar memória/contexto por padrão.

## API oficial e semântica

É usado `wss://api.deepgram.com/v1/listen`, sem SDK adicional ou endpoint
self-hosted. PCM chega em frames binários enquanto o usuário fala.
`KeepAlive` é texto JSON a cada 3 segundos sem envios. `CloseStream` drena
finals/Metadata e fecha o socket; timeout ou ausência de Metadata invalida a
montagem remota e aciona replay local.

Referências oficiais consultadas em 2026-09-04:

- [Streaming Listen API](https://developers.deepgram.com/reference/speech-to-text/listen-streaming)
- [Modelos e idiomas](https://developers.deepgram.com/docs/models-languages-overview)
- [Smart Format](https://developers.deepgram.com/docs/smart-format)
- [Numerals](https://developers.deepgram.com/docs/numerals)
- [UtteranceEnd](https://developers.deepgram.com/docs/utterance-end)
- [Endpointing e interim results](https://developers.deepgram.com/docs/understand-endpointing-interim-results)
- [KeepAlive](https://developers.deepgram.com/docs/audio-keep-alive)

Smart Format também ativa pontuação; desligar somente `punctuate` não a remove
quando Smart Format continua ligado. Numerals suporta pt-BR e é enviado junto
com Smart Format; o efeito sobre GPIO, baud e números falados precisa ser medido
no áudio real, sem correções artificiais para favorecer um provider. Redact off
é representado por **omissão** do parâmetro (a API espera tipos de entidades).
UtteranceEnd é omitido quando interim_results=false, mantendo o valor salvo
para reativação futura. Keyterms repetem o parâmetro `keyterm`, sem weights.

`is_final` consolida um segmento; `speech_final` marca endpointing. Nenhum desses
eventos isoladamente decide o fim absoluto da conversa. Os segmentos finais
são montados por timestamps e words antes do turno; duplicatas de intervalos e
palavras sobrepostas são suprimidas, preservando repetições em tempos distintos.
O fim local da captura drena a montagem. A escuta contínua envia os mesmos frames
de silêncio por até `utterance_end_ms + 1100` ms quando necessário para permitir
o evento oficial, mantendo os controles locais de pausa, limite e cancelamento.
Uma soltura manual muito rápida pode encerrar o stream antes de UtteranceEnd.

O contrato canônico possui text, is_final, speech_final, confidence opcional,
started_at/ended_at (segundos relativos ao stream), provider, language,
words/timestamps, utterance_id e sequence. Os eventos transitórios são
`interim`, `final` de segmento, `speech_started`, `speech_final`, `utterance_end`
e `state`. Permanecem no stream local autenticado, sem EventBus history ou Memory
V2. Só a frase consolidada e aceita segue para o ConversationEngine; o evento
USER_SPEECH_FINAL permite apresentar a mesma mensagem no painel sem duplicá-la.

## Bridge, limites e falhas

POST `/api/stt/ticket` gera ticket local de uso único, TTL 15 s, até 8 pendentes.
O cliente manda o ticket no primeiro frame de `/api/stt/stream`, nunca na URL.
O servidor exige peer loopback, Host/Origin confiáveis e lease de listening
válido para esse modo; uma sessão STT ativa por backend. O ticket contém modo,
formato e autorização de benchmark. Um Satellite futuro pode usar esse mesmo
contrato sem receber a Deepgram API key. O contrato antigo `/api/ws` permanece.

No Tauri, comandos `stt_stream_open/audio/close` usam o mesmo socket loopback
permitido pelo transporte existente; ownership é vinculado à janela e stream.
O navegador em desenvolvimento usa o proxy WebSocket Vite. A fronteira local
continua bloqueando acessos cross-site; nenhum listener novo na LAN é criado.

Filas: 32 frames no backend e bridge nativo; frames até 32768 bytes; até 256 KiB
pendentes no frontend; replay em memória de no máximo 60 segundos. Overflow
remoto invalida a montagem e usa o mesmo áudio completo no fallback, sem produzir
transcript de áudio com lacunas. Um erro no bridge local aborta com indicação;
não se repete automaticamente um turno de resultado ambíguo.

Falhas remotas usam cooldown exponencial de 5/10/20/40/60 s, com nova conexão em
uma próxima frase, sem loop de reconexão. AUTH_ERROR e configuração rejeitada
aguardam atualização de chave/settings. Trocas de provider/opções e remoção de
chave fecham o STT remoto, preservam o áudio corrente para fallback, e aplicam a
nova preferência na próxima frase. O microfone e a KAZUMI não são reiniciados.

Startup não conecta à nuvem e não bloqueia offline. READY significa configurado
e apto a tentar conectar; STREAMING exige socket aberto. NETWORK_ERROR só é
declarado após falha real. Fallback disponível e modelo carregado são indicadores
distintos. O pacote Faster-Whisper não é prova de modelo já baixado: em um host
novo, provisionar o modelo local antes de depender dele offline.

Shutdown fecha o bridge nativo antes do backend, fecha sockets, cancela receiver/
keepalive/sender, drena filas e apaga os buffers. Uma inferência CTranslate2 nativa
já iniciada não pode ser interrompida com segurança; seu worker é aguardado pelo
adaptador, limitado ao sample, e o lifecycle oficial mantém seu prazo de saída.
X continua hide-to-tray; Tray Exit/quit_kazumi usam o mesmo coordenador de full exit.

## Diagnóstico e validação

Diagnostics mostra formato real, provider/conexão, fallback, duração/bytes,
overflows, duplicatas suprimidas e relógios de áudio/interim/final/VAD. Latências
MIC → interim/final usam o timestamp do primeiro frame no mesmo host, não ficam
no chat e não são medidas do fim da fala. Sem resultado, mostram “não medido”.

MICROPHONE TEST usa a captura já existente em modo diagnostic, mostra interim e
final, não chama LLM nem ações. A opção de comparação manda **o mesmo buffer**
para ambos quando Deepgram estiver selecionado/configurado. Referências incluem
fala técnica, casual, pausas, números e palavrões. Calcula WER/CER normalizados e
acerto de termos; referência vazia deixa WER/CER não medidos. Não salva WAV ou
transcript do benchmark. Ao terminar, apaga o buffer; não publica áudio.

As rotas de upload legadas e Voice Lab continuam usando Faster-Whisper local;
não são apresentadas como streaming. O caminho principal de microfone usa a nova
camada nos dois modos. Não há Natural Conversation Runtime, Hardware Engine,
redesign de páginas, nem mudança de TTS nesta implementação.

Na implementação inicial não havia credencial Deepgram no Broker do usuário.
Testes de protocolo são controlados e não comprovam autenticação, precisão ou
latência reais da nuvem. Estes exigem credencial, conexão e fala natural do
operador; o relatório deve distinguir PASS de implementação de testes reais
NOT_TESTED. Voice Hunter tem duas falhas anteriores ao patch: o teste espera 12
candidatas e o catálogo do HEAD baseline fornece 13, reproduzido em snapshot
isolado sem modificar a árvore canônica.

### Resultado da execução de 2026-09-04

- Implementação local: commit `7655624108aa5b6caec600ffa19c62382a3ca3cc`.
- Backend STT/voz/conversação/TTS: **92 passed**. A suíte adicional Voice Hunter
  registrou 8 passed e 2 falhas; ambas foram reproduzidas no baseline.
- Frontend completo: **134 passed**; Rust: **24 passed**; frontend build e
  `git diff --check`: PASS.
- Build canônico `npm run build`: PyInstaller reconstruído e validado;
  Tauri release e instalador NSIS gerados, sem instalar dependências novas.
- `KAZUMI.lnk` abriu o Tauri release em `<REPO_ROOT>`, com as telas novas presentes.
  A WebView enumerou o microfone USB e os dispositivos virtuais. Nenhuma amostra
  foi capturada: a validação foi interrompida antes do teste de áudio.
- O backend empacotado carregou o Faster-Whisper `tiny`, mas o Windows Defender
  interrompeu o processo antes do serviço HTTP ficar disponível. Detecção
  `Trojan:Win32/Bearfoos.B!ml`, ThreatID `2147731849`, às 21:10:50; quarentena
  confirmada às 21:11:04. A cópia de packaging foi também detectada às 21:12:23
  e colocada em quarentena às 21:12:35. O histórico contém a mesma detecção em
  builds de 3 e 4 de setembro anteriores a esta mudança. Isso não determina se
  a detecção é ou não um falso positivo; é necessária análise da proteção local.
- Os caminhos afetados foram `desktop/src-tauri/target/release/backend-runtime/
  kazumi-backend.exe` e `packaging/dist/kazumi-backend/kazumi-backend.exe`. O instalador
  gerado não representa uma validação de execução bem-sucedida. Não foram
  alteradas configurações do Defender, exclusões ou itens em quarentena.
- `quit_kazumi` encerrou o desktop de validação; backend=0, desktop=0, porta
  8000 livre. Isso **não comprova shutdown/restart de uma sessão STT saudável**,
  porque o backend já havia sido interrompido pela proteção.
- Credential Broker do usuário real: **NOT_CONFIGURED**. Autenticação Deepgram,
  WebSocket cloud, fala natural pt-BR, latências reais, comparação de áudio e
  startup offline do release: **não validados**. Fallback de autenticação/rede,
  lifecycle, montagem e persistência passaram em testes controlados.

Estado da camada de código: **PASS WITH LIMITATIONS**. Estado final da validação
oficial: **FAIL / bloqueado**, até analisar a detecção e provisionar a credencial
pelo Broker. Nenhum áudio ou segredo foi versionado ou publicado.
