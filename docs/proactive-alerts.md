# Alertas proativos

Fluxo:

```text
Network Watch -> Event Bus -> Proactive Presence -> UI/chat | voz opt-in
```

Telemetria comum não chega ao LLM. Regras locais publicam mudanças sustentadas,
falhas e recuperações; o Proactive Presence decide relevância, cooldown,
coalescência e canal. A apresentação visual é o fallback; voz depende do opt-in
`Voice proactive` e do modo atual.

Alertas críticos têm prioridade sobre warnings. A conversa do usuário usa a
prioridade `USER` e sínteses são serializadas para evitar sobreposição. A fala
proativa não começa enquanto usuário ou assistente estão falando/ouvindo e
passa pelo mesmo Prosody/Pronunciation Engine e TTS com fallback.

`POST /api/network-watch/debug` aceita somente eventos predefinidos pelo
schema local. Ele valida latência, falhas e recovery sem derrubar a interface;
todo evento desse caminho recebe `simulated=true` e não altera métricas reais.
