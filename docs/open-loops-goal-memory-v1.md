# Open Loops & Goal Memory V1

## Contrato

`app.open_loops.OpenLoopEngine` mantém objetivos e continuidades futuras no
SQLite local configurado da KAZUMI. Open Loop não é Task: ele lembra o que está
incompleto, aguardando ou bloqueado, sem agendar ou executar nada. Tasks mantêm
seu scheduler existente e podem referenciar um único `goal_id` principal.

Estados de Open Loop: `OPEN`, `ACTIVE`, `WAITING`, `BLOCKED`, `RESOLVED`,
`CANCELLED` e `STALE`. Goals usam lifecycle próprio derivado dos loops e ficam
em `ACTIVE`, `PAUSED`, `RESOLVED`, `CANCELLED` ou `STALE`.

## Criação e consolidação

Eventos estruturados de Task Engine, Operator/Agent Runs, MonitorJobs e SelfDev
criam ou atualizam continuidade automaticamente. Artifact Context somente
anexa metadados verificados a um loop relacionado; não cria pendência para cada
arquivo. Frases explícitas do operador como “depois eu testo”, “ainda falta” ou
“estou aguardando” passam por uma política determinística. Saudações,
agradecimentos, comandos pontuais e perguntas já respondidas não criam loops.

A consolidação usa relações exatas (Task, Monitor e artefato), projeto, entidade
e similaridade lexical. Uma atualização deduplicada preserva o mesmo ID e
acrescenta relações/contexto, mantendo o histórico de transições.

## Grounding e segurança

`RESOLVED` exige `ResolutionEvidence(verified=true)` de tipo permitido. São
aceitos efeito de Task/tool verificado, condição de MonitorJob atingida,
artefato verificado, pós-validação SelfDev ou confirmação explícita do operador.
Texto livre do LLM e fontes `llm`/`assistant`/`model`/`prompt` não são evidência.
Timeout ou falha de MonitorJob leva a `BLOCKED`, não a resolução falsa.

Open Loops não expõem nenhuma operação de execução. Approval Gate, classificação
de risco, Action Budget, Credential Broker, Trusted Host Registry e grounding
continuam pertencendo às autoridades atuais.

## Retomada e World State

A resolução contextual combina recência, prioridade, projeto, entidade,
assunto, source turn e artefatos relacionados. O Resume Context contém somente:

- objetivo e estado;
- último estado confirmado e última ação;
- bloqueio/condição aguardada;
- referências de artefatos, sem copiar conteúdo;
- próximo passo possível.

O World State persiste apenas `active_goal`, `open_loop_count`,
`waiting_loop_count` e `most_relevant_open_loop`. A lista completa não entra no
prompt. A Operations UI reutiliza a Overview e mostra Open, Waiting, Blocked e
Recent resolved com detalhes expansíveis.

## Persistence, Memory V2 e API

Migration Intelligence V3 adiciona `goals_v1`, `open_loops_v1` e
`open_loop_history`. Linhas terminais e STALE permanecem rastreáveis. A política
de stale considera tempo, projeto encerrado e goal superseded. Uma resolução
grounded gera memória episódica consolidada no Memory V2; a memória registra o
que ocorreu, enquanto o Open Loop registra o que ainda precisa ocorrer.

Rotas locais sob `/api/intelligence`:

- `GET|POST /goals`;
- `GET|POST /open-loops` e `GET /open-loops/{id}`;
- `POST /open-loops/{id}/transition` e `/resume`;
- `GET /open-loops/actionable`, `/waiting`, `/recent-resolved` e `/priority`.

As APIs futuras `get_actionable_loops()`, `get_waiting_loops()`,
`get_recent_resolved()` e `get_priority()` já estão disponíveis no serviço para
um futuro Proactive Presence Engine, sem implementar proatividade nesta versão.
