# NYRA Intelligence Platform V2

## Escopo e princípios

`app.intelligence` integra memória seletiva, conhecimento local, contexto,
roteamento de modelos, capabilities, Skills, tasks, eventos, diagnósticos,
traces, replay, fronteiras de confiança, budgets e visão local opcional. A
plataforma compõe as autoridades existentes; não substitui `ApprovalGate`,
`CredentialBroker`, `ToolRegistry`, `DesktopController`, Browser CDP,
`AgentLoopRuntime` nem `SelfDevelopmentService`.

O runtime continua local-first. O armazenamento usa o SQLite já configurado em
`Settings.database_path`, separado por tabelas lógicas e migrations. RAG usa
feature hashing local e não requer banco vetorial ou serviço cloud. Conteúdo de
documento, memória e web entra no contexto como dados com trust boundary, nunca
como system instruction.

## Fluxo integrado

```text
User request
  -> ContextBuilder
     -> Memory V2 retrieval
     -> local RAG retrieval + provenance
     -> live Capability Registry
     -> bounded Context Engine
  -> Model Router V2 (live Ollama inventory + RAM + task capabilities)
  -> existing planner / policy / tools
  -> effect verification
  -> redacted runtime trace + Event Intelligence
  -> user-facing response
```

O system prompt e as políticas ficam fora do budget de contexto. A falha de uma
fonte V2 degrada a montagem sem remover as instruções de segurança.

## Memory V2

Tipos: working, conversation, episodic, semantic, project, operational,
user-preference e tool-history. Cada item inclui timestamp, source, category,
project, confidence, relevance, sensitivity, expiration, decay, provenance e
entidades relacionadas.

- Working memory fica somente no processo e é limitada.
- Conversation memory abaixo do limiar de relevância não persiste.
- Conteúdo que parece secret e itens `SECRET` são rejeitados; `SENSITIVE`
  requer um armazenamento especializado/opt-in.
- Hash de conteúdo deduplica; fatos materialmente diferentes da mesma
  entidade/categoria são mantidos e ligados como conflito.
- Recuperação combina overlap lexical, confidence, relevance e time decay.
- Credenciais permanecem exclusivamente no Credential Broker.

## RAG local / Knowledge Engine

Formatos suportados: Markdown, TXT, código, JSON, YAML e logs. PDF é habilitado
somente quando `pypdf` está disponível. Paths são resolvidos e precisam estar
dentro dos roots autorizados (`PROJECT_ROOT` e `DATA_ROOT` no runtime padrão).

O pipeline normaliza, divide com overlap, produz embedding determinístico leve,
grava metadata/provenance e faz retrieval + rerank. SHA-256, tamanho e mtime
evitam reindexação de arquivo inalterado. Cada hit é `DOCUMENT_CONTENT`.

## Context Engine

O budget deriva do contexto configurado do Ollama, limitado entre 4.000 e
24.000 caracteres. Blocos são ranqueados por prioridade e relevância. As
decisões SELECT/DROP_BUDGET ficam disponíveis nos diagnósticos e na Operations
UI. Conversa, memória, documentos e runtime mantêm trust boundary explícito.

## Model Router V2

O router consulta `/api/tags` e `/api/ps` pelo `BrainManager`, classifica modelos
instalados como general/reasoning/coding/fast/long-context/vision/classification
e considera RAM, residência, modelo oficial e preferências. A seleção é por
chamada; não muda o modelo oficial global. Se o router falhar, o Brain usa o
modelo oficial e o fallback configurado. Decisões ficam em `last_model_route`.

## Skills e Capability Registry

Manifests YAML/JSON em `config/skills` declaram identidade, capabilities, tools,
permissions, risco, dependências, health checks, ações e validators. Descoberta
é dinâmica, mas execução só ocorre se houver Skill real no registro legado.

O Capability Registry agrega os probes V1 e os novos componentes e usa somente:
`AVAILABLE`, `DEGRADED`, `OFFLINE`, `DISABLED`, `UNCONFIGURED`, `BLOCKED` e
`UNKNOWN`. Dependência ausente bloqueia a capability. A resposta para “o que
você consegue fazer?” deve vir de `/api/intelligence/capabilities/summary`.

## Autonomous Task Engine

Tasks persistem no SQLite e usam estados CREATED, QUEUED, RUNNING, WAITING,
WAITING_APPROVAL, COMPLETED, FAILED, CANCELLED e PAUSED. Suporta one-shot,
schedule ISO, recorrência `every:<seconds>`, evento e condição no contrato.
Reinício recupera RUNNING como QUEUED. Execução tem concorrência, timeout,
retry/backoff, capability gate, risk gate e exige `effect_verified=true`.

Triggers `event` permanecem em `WAITING` até um evento do `EventBus`
corresponder deterministicamente a `event_type` e, opcionalmente,
`source`/`payload_equals`. Triggers `conditional` avaliam
`capability_available`, `capability_states` e `not_before`; condições sem
semântica suportada são rejeitadas. Nenhum texto livre de LLM é avaliado como
condição. Identidade, estado, retries, resultado e timestamps são sempre
gerados/resetados pelo servidor, e `approval_mode=always` permanece em
`WAITING_APPROVAL`.

O V2 registra inicialmente apenas handlers controlados: diagnóstico read-only,
snapshot de capabilities e ingest incremental dentro do allowlist. Ações
sensíveis continuam pertencendo aos engines existentes e aos approvals de uso
único; um booleano de API não concede aprovação.

## Event Intelligence e Diagnostics

Eventos preservam source, category, severity, entity, payload, correlation ID,
evidence level e confidence. O engine usa `OBSERVED`, `CORRELATED`, `INFERRED` e
`CONFIRMED`; correlação nunca define causalidade. Telemetria normal repetitiva
não vira incidente e categorias de alta frequência são coalescidas. A fila é
limitada e falhas de persistência não matam o worker.

Diagnostics executa checks registrados e limitados por timeout. O resultado
inclui diagnosis, probable cause, confidence, evidence, passed/failed checks e
ação recomendada. Nenhum texto livre do LLM é tratado como evidência. Domínios
atuais: NYRA, Ollama, memory, RAG, desktop, browser, voice, network e SelfDev.

## Browser, Desktop e Vision

Browser V2 continua CDP-first, com DOM semântico, waits e approvals para
interações elevadas. DOM e resultados de busca agora declaram `WEB_CONTENT`,
`instruction_authority=false` e flags de prompt injection. Cookies, storage,
tokens e passwords continuam protegidos.

Desktop mantém descoberta canônica, UI Automation e effect verification. Vision
estrutural existente captura frames escopados, inspeciona UIA/pixels e compara
before/after. `LocalVisionAdapter` é opcional: seleciona somente modelo vision
realmente instalado no Ollama, mantém a imagem local e instrui o modelo a apenas
observar. Sem modelo, reporta `UNCONFIGURED`; visão estrutural continua ativa.
O endpoint do modelo visual aceita apenas Ollama em loopback/localhost. Leitura
por path exige arquivo dentro de `PROJECT_ROOT` ou `DATA_ROOT` após resolução
canônica; tentativas fora desses roots são bloqueadas antes da leitura.

## Trace, Replay, trust e Action Budget

Traces suportam USER_REQUEST, CONTEXT_ASSEMBLY, MODEL_ROUTE, PLAN,
POLICY_DECISIONS, TOOL_CALL, TOOL_RESULT, VERIFICATION, RETRY, FINAL_DECISION e
RESPONSE. O EventBus alimenta uma fila limitada. Payloads são redacted. Replay
é dry-run por padrão e nunca repete TOOL_CALL/TOOL_RESULT cegamente; ações
destrutivas reexecutadas = zero.

Trust boundaries: SYSTEM_TRUSTED, USER_INPUT, TOOL_TRUSTED, TOOL_UNTRUSTED,
REMOTE_CONTENT, WEB_CONTENT, DOCUMENT_CONTENT e MEMORY_CONTENT. Detecção de
prompt injection é sinal adicional; a separação estrutural é a defesa primária.

`ActionBudget` centraliza tool calls, retries, planner iterations, falhas,
timeout, restart e budgets destructive/network. O Agent Loop preserva seus
limites e permite apenas a verificação read-only final já prevista.

Diagnósticos do Context Engine persistem somente budget, contagens e decisões
SELECT/DROP; nunca copiam o conteúdo selecionado. Conteúdo RAG passa pelo
redactor antes de hash, chunking e persistência. Saídas resumidas de validação
SelfDev também são redacted.

## SelfDev V2 e Evaluation Suite

O lifecycle anterior é preservado e registra novos gates: REPRODUCE,
ROOT_CAUSE_ANALYSIS, STATIC_ANALYSIS, REGRESSION_BENCHMARK,
CANARY_VALIDATION e BEHAVIOR_COMPARISON. O baseline roda no mirror
`<SELFDEV_WORKSPACE>/repository`; nunca no runtime operacional. Promoção ainda
exige árvore estável limpa, política de risco, security gate, cherry-pick,
restart, post-validation e rollback por `git revert`.

Evaluation Suite registra cenários REAL/SIMULATED/MOCKED sem conflar os três e
mede correctness, grounding, tool success, verification, safety e latência. Os
relatórios JSON e Markdown ficam em `reports/evaluations` do runtime local.

## Persistence e migrations

Schema Intelligence atual: versão 2. Tabelas lógicas:

- `memory_v2` e `memory_v2_fts`;
- `knowledge_documents`, `knowledge_chunks`, `knowledge_fts`;
- `autonomous_tasks_v2`;
- `intelligence_events`;
- `execution_traces`;
- `intelligence_schema`.

Migrations usam transação `BEGIN IMMEDIATE`, WAL e foreign keys. `quick_check`
é exposto no status. Estado privado, banco, traces e relatórios nunca entram no
pacote ou no snapshot público.

## API local

Todas as rotas ficam sob `/api/intelligence` e herdam o middleware local:

- `GET /status`, `/capabilities`, `/capabilities/summary`, `/skills`;
- `POST /memory`, `/memory/search`; `DELETE /memory/{id}`;
- `POST /rag/ingest`, `/rag/search`, `/context/assemble`, `/model/route`;
- `GET|POST /tasks...` para create/run/pause/resume/cancel;
- `GET /events`, `/incidents`, `/traces`;
- `POST /diagnostics/{domain}`, `/traces/{id}/replay`;
- `POST /evaluations/run`, `/vision/analyze`.

## Falhas e degradação

Ollama offline impede rota/vision model, mas não corrompe memória. DB offline é
reportado pelo health e workers continuam isolados. Handler de task sem efeito
verificado falha. Context/RAG indisponível não desloca system policy. Browser ou
desktop indisponível retorna capability/verification failure. Toda espera tem
timeout; filas são limitadas; nenhum loop é infinito.

## Observabilidade

A Overview da Operations UI consulta `/api/intelligence/status` com no-store.
Sem resposta real, mostra UNKNOWN/telemetria indisponível. São exibidos Brain,
modelo, Memory, RAG, Context, Tasks, Events, Trace, Skills, Browser, Desktop,
Vision, Diagnostics e SelfDev sem secrets e sem status fabricado.
