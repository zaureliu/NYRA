# Provedores de voz online opcionais

A camada V1 separa a intenção de fala da geração do áudio:

```text
Conversation / LLM
  -> SpeechTextNormalizer
  -> VoiceStylePlan
  -> TtsProviderRegistry
       -> LocalTtsProvider
       -> OpenAITtsProvider
       -> ElevenLabsTtsProvider
  -> SpeechQueue
  -> playback único
  -> lip sync / dispositivo global
```

O default absoluto é `provider=local`, `fallback_provider=local` e
`online_enabled=false`. Uma instalação nova ou offline não precisa de conta,
API key, créditos ou Internet. Com o toggle desligado, status, health e catálogo
usam apenas estado local e não fazem chamadas aos providers externos.

## Configuração

Em **Settings > Voice**, ative `Enable Online Voice Providers`, escolha OpenAI
ou ElevenLabs, salve a API key pelo botão `Save securely`, selecione modelo/voz
e use `TEST PROVIDER`. O teste nunca roda automaticamente e pode consumir
créditos do serviço. A seleção é aplicada sem rebuild.

Preferências não secretas são persistidas no runtime: provider selecionado,
toggle online, fallback, modelo e voz. As chaves lógicas
`tts_openai_api_key` e `tts_elevenlabs_api_key` vivem exclusivamente no
Credential Broker (Windows Credential Manager com fallback DPAPI). A API e o
frontend recebem apenas `configured=true/false`; o valor salvo nunca é lido de
volta pela UI.

## Contratos e privacidade

`TtsProvider` expõe identidade, capabilities, health local, validação,
catálogos, síntese e cancelamento. `TtsProviderRegistry` é a única autoridade
de seleção e fallback; Conversation, SpeechQueue, playback, lip sync e Desktop
Presence não possuem branches específicos para OpenAI ou ElevenLabs.

O request online contém somente `speech_text` final e os campos necessários de
modelo, voz, formato, velocidade e estilo suportado. Histórico, system prompt,
Memory, RAG, World State, tools, arquivos, telas, logs e credenciais nunca fazem
parte do payload de TTS. O normalizador remove Markdown, JSON interno, traces,
URLs, paths e identificadores de runtime antes da síntese.

Falhas são classificadas como `DISABLED`, `NOT_CONFIGURED`, `AUTH_ERROR`,
`QUOTA_ERROR`, `RATE_LIMIT_ERROR`, `NETWORK_ERROR`, `DEGRADED` ou
`PROVIDER_ERROR`. Auth/quota não recebem retry infinito. Falha, timeout ou rede
indisponível acionam o fallback local no limite seguro do chunk. Cancelamento de
turno cancela request/fila; qualquer resultado tardio é descartado pela mesma
ownership de `turn_id`/`response_id` já usada pela voz local.

## APIs internas seguras

- `GET /api/audio/providers`: metadata, capabilities e estado; sem I/O externo.
- `PUT /api/audio/providers/settings`: persiste toggle, seleção, modelo e voz.
- `PUT|DELETE /api/audio/providers/{id}/credential`: grava/remove no broker.
- `POST /api/audio/providers/{id}/catalog/refresh`: consulta explícita do operador.
- `POST /api/audio/providers/{id}/test`: síntese curta explícita pela SpeechQueue.

Os adapters seguem as APIs oficiais atuais:

- OpenAI Speech: <https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create>
- OpenAI TTS guide: <https://developers.openai.com/api/docs/guides/text-to-speech>
- ElevenLabs TTS: <https://elevenlabs.io/docs/api-reference/text-to-speech/convert>
- ElevenLabs models/voices: <https://elevenlabs.io/docs/api-reference/models/list> e <https://elevenlabs.io/docs/api-reference/voices/search>

Streaming permanece declarado como `false` nesta versão: os adapters produzem
um chunk normalizado por vez e o entregam à SpeechQueue existente. Adicionar um
provider futuro exige apenas outro adapter registrado no mesmo contrato.
