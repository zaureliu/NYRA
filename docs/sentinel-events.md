# Sentinel Events

O transporte escolhido é o Flask-SocketIO já existente no Sentinel, no namespace `/integrations/nyra`. O evento é `sentinel_event`. A versão atual do protocolo é 1. No servidor Werkzeug/threading atual, o cliente fixa Engine.IO long-polling porque o upgrade WebSocket fecha com erro no stack instalado; isso preserva push em tempo real e heartbeat sem reexecutar discovery. Uma implantação Socket.IO com servidor WebSocket compatível poderá habilitar upgrade futuramente.

```text
Sentinel alert/broadcast
  -> NyraEventAdapter (allowlist + schema v1)
  -> Socket.IO /integrations/nyra
  -> SentinelConnector
  -> schema validation + size limit + dedupe
  -> SQLite sentinel_events + NYRA Event Bus
  -> dashboard / desktop bubble / proactive decision
  -> pronunciation engine -> SpeechQueue -> TTS
```

Cada evento possui `event_id`, `instance_id`, timestamp, category, type, severity normalizada, title, summary, entity e metadata sanitizada. IDs processados ficam em cache curto; o SQLite usa `event_id` como chave primária. Reconnect consulta até 100 eventos do buffer de replay da sessão do Sentinel e não fala replay.

Severity: `info`, `warning`, `critical`, `recovery`. Info é normalmente visual; warning obedece Voice Alerts; critical recebe prioridade alta; recovery só é falado quando relacionado a um incidente anteriormente falado. Quiet Mode global suprime voz automática.

Eventos continuam no histórico mesmo quando cooldown, Critical Only ou Quiet Mode suprimem a fala. O overlay recebe apenas balão temporário; não ganha card permanente.

Limite atual: o Sentinel conserva replay em memória de até 500 eventos da sessão. A NYRA persiste até a retenção configurada, padrão 30 dias.
