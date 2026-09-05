# Proactive Presence Engine V1

O Proactive Presence permite que a NYRA apresente uma mudança relevante sem
depender de uma nova mensagem do operador. O engine não detecta condições por
conta própria: consome o Event Bus já alimentado por World State, Open Loops,
Tasks, MonitorJobs, USB Monitor, Network Watch, Proxmox, OpenWrt/Home Assistant,
Sentinel, SelfDev, Artifact Context, Runtime Supervisor e Operator.

## Fluxo

```text
fonte existente -> EventBus -> policy/normalização -> relevância + contexto
                                            -> cooldown/dedup/incidente
                                            -> decisão persistida
                                            -> silêncio | UI | chat | voz | defer
```

`ProactiveDecisionEngine` produz somente `IGNORE`, `LOG_ONLY`,
`UI_NOTIFICATION`, `CHAT_MESSAGE`, `VOICE_AND_CHAT` ou `DEFER`. Impacto,
urgência, goal/loop ativo, pedido recente, novidade, repetição, confiança,
freshness, atividade do operador e estado da assistente entram na decisão.
Eventos `LOW` não interrompem; `CRITICAL` é reservado a sinais explicitamente
classificados, como crash loop de runtime ou alerta crítico do Sentinel.

## Cooldown, coalescência e recuperação

Cada apresentação abre cooldowns persistentes por chave semântica, tipo,
entidade, fonte e goal. Eventos equivalentes são auditados e consolidados sem
uma nova mensagem. Recuperações de rede, runtime ou integração só aparecem se o
incidente correspondente foi relevante e apresentado antes. Existe também um
limite conservador por hora; candidatos excedentes podem ser adiados.
Adiamentos causados por esse limite recebem um `retry_not_before` persistente,
evitando reavaliação em cada mudança de atividade. Adiamentos porque usuário ou
assistente estão ocupados continuam elegíveis assim que o estado é liberado.

Snapshots rotineiros e frequentes de Network Watch usam um fast-path de
silêncio contabilizado, sem enfileiramento ou escrita SQLite. Somente as
transições sustentadas produzidas pelas regras existentes viram candidatos.

## Atenção e modos

- `NORMAL`: eventos normais relevantes usam UI ou chat conforme atividade;
- `QUIET`: somente `HIGH` e `CRITICAL` podem ser apresentados;
- `DO_NOT_DISTURB`: somente `CRITICAL` pode ser apresentado;
- durante `THINKING`, `ACTING`, `SPEAKING` ou `LISTENING`, o evento é adiado,
  salvo um crítico, que usa chat sem sobrepor voz;
- voz proativa é desligada por padrão, exige provider local pronto e nunca
  depende do Voice Satellite.

Os controles mínimos já usam Settings > Automation; não existe nova sidebar.

## Persistência e APIs locais

Decisões, notificações, cooldowns, incidentes e fila adiada usam tabelas
separadas no SQLite da Intelligence Platform. A fila EventBus residente é
limitada a 256 itens e nenhum polling ou scheduler paralelo foi criado.

- `GET /api/proactive-presence/status`
- `GET|PUT /api/proactive-presence/settings`
- `GET /api/proactive-presence/notifications`
- `POST /api/proactive-presence/notifications/{id}/read`
- `GET /api/proactive-presence/decisions`

Somente `PROACTIVE_PRESENCE_NOTIFICATION`, já decidido, chega à apresentação do
frontend. O histórico técnico e os scores ficam internos.

Monitor e Open Loops consomem o mesmo evento em filas independentes. Uma
tentativa curta e limitada de resolução da relação evita depender da ordem dos
subscribers e permite anexar `goal`/`open_loop` sem bloquear o Event Bus.

## Segurança

O engine só apresenta contexto. Ele não chama tools, não agenda Tasks, não
executa a próxima ação e não concede aprovação. O payload final fixa
`execution_authorized=false` e `action_budget_consumed=0`; o frontend também
ignora qualquer tentativa de alterar esses campos. Uma resposta como
“continua então” volta pelo fluxo normal de Open Loops/World State e permanece
sujeita a Grounding, Action Budget, Credential Broker, risk policy e approvals
de uso único.
