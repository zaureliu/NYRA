# World State Engine V1

`app.world_state.WorldStateEngine` mantém uma visão operacional local,
compartilhada e de baixo custo. Ele não descobre nem executa ações: agrega
observações verificadas das autoridades já existentes.

## Fontes

- Win32 via `ComputerPerceptionService`: foreground, janela, processo,
  aplicativos recentes, arquivos recentes e atividade do usuário;
- EventBus: transições de desktop, tasks, MonitorJobs, USB, Network Watch,
  conversa, assistente, Sentinel e Homelab;
- `RecentArtifactMemory`: somente referências/metadados de artefatos cuja
  existência ou criação foi verificada;
- Tool Registry: resultados estruturados e verificados de browser e
  integrações.

Texto do usuário, memória e resposta livre do LLM nunca são fontes de estado.
Cada valor exposto contém `value`, `source`, `observed_at`, `confidence`,
`freshness` e `verified`. Ao vencer o TTL, o valor deixa de ser exposto como
atual; sua proveniência permanece disponível para diagnóstico como
`STALE`/`EXPIRED`.

## Persistência e privacidade

O arquivo `%LOCALAPPDATA%\KAZUMI\data\world-state-v1.json` usa substituição
atômica e guarda apenas referências úteis entre restarts: projeto, arquivos e
artefatos recentes, tasks e monitores ativos, além de uma timeline seletiva e
curta. Foreground, janela, browser, atividade do usuário e estado da assistente
não são restaurados como atuais. Conteúdo de arquivos, áudio, texto de conversa,
secrets, IPs e MACs não são persistidos.

## Consumidores

- Context Engine seleciona um bloco compacto `[WORLD STATE]` somente quando o
  pedido se relaciona ao estado observado;
- Universal Operator consulta `current_focus` antes do fallback local de
  resolução de pronomes;
- `GET /api/world-state` e a Operations UI exibem foco, aplicativo, contagens,
  eventos e freshness sem fazer nova descoberta global.
