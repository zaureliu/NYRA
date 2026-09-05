# Privacidade

- Always Listening é desligado por padrão e exige ativação explícita.
- O indicador de microfone permanece visível no dashboard e no overlay.
- O áudio do usuário é segmentado, validado e transcrito localmente; gravações contínuas não são armazenadas.
- Arquivos temporários de utterances são removidos após processamento.
- `Ctrl+Shift+M`, tray e Settings permitem mutar; mute encerra a captura real.
- Fala sem wake word é descartada antes do LLM quando não há sessão hands-free.
- Edge TTS recebe somente `speech_text` da resposta da NYRA, nunca áudio do microfone, memória completa ou topologia.
- OpenAI e ElevenLabs TTS ficam desligados por padrão. Mesmo com uma credencial
  salva, o toggle `Enable Online Voice Providers` desligado garante zero requests
  de voz externos, inclusive health e catálogo. Quando ativado, somente o
  `speech_text` final e modelo/voz/estilo necessários são enviados; histórico,
  prompts, Memory, RAG, World State, tools, arquivos, telas e logs não são enviados.
- API keys de voz ficam no Credential Broker e nunca em configuração, SQLite,
  localStorage, resposta da API, trace ou log. A UI recebe somente o estado
  `Configured`/`Not configured`.
- Network Watch permanece read-only. Separadamente, `system_shell` pode executar diagnósticos locais escolhidos por tool calling nativo; comando e saída são redigidos para padrões conhecidos de secrets antes de logs, eventos, histórico e retorno ao modelo.
- `remote_shell` não envia private keys, passwords, SSH agent, username ou known_hosts ao modelo. Somente host lógico, capability e resultado redigido entram no contexto. Host keys nunca são aceitas automaticamente após mudança.
- Agent Runs persistem metadata operacional e resumos limitados, não outputs integrais nem chain-of-thought.
- Logs guardam métricas operacionais e tipos de evento, não áudio nem conversas completas.
- `logs/shell.log` guarda comando redigido e metadados de auditoria. O histórico SQLite não guarda stdout/stderr. O ambiente do processo é herdado para compatibilidade, mas nunca é anexado automaticamente ao prompt.

## Utamo Sentinel

Sentinel Watch fica OFF até opt-in explícito. Quando ativo, envia ao Sentinel apenas fingerprint requests, autenticação com token dedicado e heartbeat Socket.IO. Recebe somente eventos sanitizados do schema público v1. O token permanece em `data/secrets/`, não é retornado ao frontend e não aparece em logs. Discovery LAN só ocorre em redes privadas allowlisted; não existe scan de Internet ou enumeração de portas. A integração é read-only e nunca altera alertas, dispositivos, regras, sensores ou banco do Sentinel.
