# Alertas proativos

Fluxo:

```text
Network Watch -> Event Bus -> ProactiveNetworkAlerts -> visual/voz -> SpeechQueue
```

Telemetria comum não chega ao LLM. Regras locais publicam somente mudanças sustentadas, falhas e recuperações. O alerta visual é emitido primeiro; voz depende de `Voice Alerts` e `Quiet Mode`.

Alertas críticos têm prioridade sobre warnings. A conversa do usuário usa a prioridade `USER` e sínteses são serializadas para evitar sobreposição. A fala passa pelo mesmo Prosody/Pronunciation Engine e TTS com fallback; se Edge TTS estiver offline, o fallback local configurado continua disponível.

Em desenvolvimento, `POST /api/network-watch/debug` aceita apenas eventos predefinidos. Ele permite validar `high_latency`, falhas e recovery sem derrubar a interface real.
