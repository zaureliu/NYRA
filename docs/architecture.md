# Arquitetura

## Camada V4

```text
Microfone/web -> STT -> RealtimeOrchestrator -> ContextSelector -> LLMProvider.stream
                              |                         |
                           EventBus          memória + percepção relevante
                              v
tokens -> SentenceAssembler -> Pronunciation -> TTS -> Voice Processor
                              v
                 SpeechQueue ordenada -> áudio -> AvatarController
                              |
        Attention / Reaction / Skills / Sentinel / Network Watch
```

`RealtimeOrchestrator` preserva o contrato anterior, mas coordena stream, sentenças, fila ordenada, telemetria e cancelamento sem conhecer providers concretos. Cada resposta possui `response_id`; em `SMART_DUPLEX`, `USER_SPEECH_STARTED` cancela stream e chunks pendentes e retorna a `LISTENING`. `HALF_DUPLEX` permanece o padrão seguro.

`PCAwareness` coleta somente sinais autorizados e efêmeros. `ContextSelector` seleciona o mínimo relevante para o prompt. `AttentionEngine`, `ReactionEngine` e `ProactiveEngine` aplicam prioridade, decay, cooldowns e orçamento de fala. `SkillRegistry` continua sendo uma allowlist tipada. Shell arbitrário existe exclusivamente como chamada nativa `system_shell`; texto livre de resposta nunca é executado.

```text
Microfone/web → STT → Orchestrator → contexto seletivo → LLMProvider
                         ↕                    ↕
                    EventBus            SQLite + FTS5
                         ↓
Resposta → Prosody/display+speech → TTSProvider → áudio → lip sync
                         ↕
        tools nativas / system_shell mediado / monitor / integrações
```

O dashboard web e o Desktop Presence Tauri consomem o mesmo FastAPI/WebSocket. `AvatarRenderer` mantém SVG, PNG, Layered e FutureLive2D atrás de um contrato visual. A V3 implementa PNG/Layered e preserva SVG V2 como fallback.

Chatterbox roda por subprocesso em `.venv-chatterbox`; Kokoro permanece no `.venv` principal. Essa fronteira evita conflito entre PyTorch/NumPy do Chatterbox e ONNX/NumPy do MVP.

O FastAPI compõe serviços independentes no `lifespan`. `ChatOrchestrator` coordena uma conversa sem conhecer Ollama, faster-whisper ou SAPI diretamente; ele depende das abstrações `LLMProvider`, `STTProvider` e `TTSProvider`.

## Contexto

Cada turno usa o system prompt, o estado emocional, o resumo das ferramentas, até 12 mensagens recentes e no máximo 6 memórias recuperadas por FTS5. O banco inteiro nunca é colocado no prompt. Dados recuperados são marcados como dados e não como instruções.

## Estado emocional

Estados: `neutral`, `happy`, `curious`, `focused`, `concerned`, `amused`, `tired` e `surprised`. Todos podem transicionar entre si. Regras determinísticas inferem um estado inicial do texto; a transição é persistida em `character_state` e publicada como `STATE_CHANGED`. Estado muda apresentação, não permissões nem decisões de segurança.

## Eventos

O barramento assíncrono publica `USER_SPEECH_RECEIVED`, `USER_TEXT_RECEIVED`, `LLM_PROCESSING`, `NYRA_RESPONSE`, `TTS_STARTED`, `TTS_FINISHED`, `HOMELAB_EVENT`, `MEMORY_CREATED`, `STATE_CHANGED` e `ERROR`. WebSocket envia esses eventos ao frontend. Todo evento originado por um turno — inclusive agent run, shell local/remoto, runtime e desktop — transporta o mesmo `turn_id`; eventos globais de monitor podem usar `turn_id=null`. O frontend rejeita eventos tardios de turnos encerrados. `scripts/e2e_turn_isolation.py` cruza a resposta HTTP com `USER_TEXT_RECEIVED`/`NYRA_RESPONSE` no WebSocket e exige `DESKTOP_WINDOW_VERIFIED` no turno de abertura física.

## Barge-in futuro

O contrato já separa reprodução, status e captura. A extensão deverá publicar `USER_SPEECH_STARTED`, cancelar a tarefa de TTS/reprodução com um token, emitir `TTS_INTERRUPTED` e preservar no histórico apenas o trecho realmente reproduzido. Cancelamento nunca deve interromper uma transação de memória no meio.

## Avatar

O manifest V3 separa assets e lógica. O renderer recebe `state`, `status`, `mouth`, variante e preferências. O `LayeredRenderer` usa base RGBA e overlays substituíveis; falhas caem no SVG V2. Uma implementação Live2D futura apenas implementa esse contrato.
### Pronunciation V3.2

O texto técnico segue separado em `display_text` e `speech_text`. `PronunciationEngine` carrega defaults + overrides, protege URLs/paths, resolve longest-match, normaliza números/unidades e aplica aliases por provider antes da prosódia. O Pronunciation Lab usa endpoints locais de preview/export/import e hot reload por síntese.
# V3.3 resident services

```text
Desktop/dashboard capture -> in-memory PCM ring -> local VAD/faster-whisper
  -> AlwaysListeningManager -> wake-word/hands-free policy -> ChatOrchestrator

NetworkWatchMonitor -> rolling metrics -> NetworkRuleEngine -> EventBus
  -> ProactiveNetworkAlerts -> SpeechQueue -> TTS fallback chain -> overlay
```

`AlwaysListeningManager`, `NetworkWatchMonitor`, `SystemShellService`, `RemoteShellService`, `AgentController` e `SpeechQueue` são serviços gerenciados pelo lifespan do FastAPI. Preferências runtime persistem separadamente dos defaults versionados. Probes estruturados continuam read-only; shells recebem somente argumentos do tool calling nativo e impedem execução sensível sem policy/approval backend.

## Tool calling e shell local

```text
User / STT / Desktop Presence
        ↓
RealtimeOrchestrator → AgentController/run_id → Ollama tools schema → ToolAgentLoop limitado
        ↓                                     ↓
   resposta final                  ToolRegistry → system_shell / remote_shell / probes
                                                ↓
                                   RiskClassifier → ApprovalGate
                                                ↓
                                   ShellExecutor → PowerShell/CMD
                                                ↓
                            stdout/stderr/exit/timeout → Ollama → resposta/TTS
```

`ShellRiskClassifier` compõe o maior impacto detectado em executável/cmdlet, argumentos, pipelines, encadeamentos, redirecionamentos, aliases, scripts e alvos sensíveis. Executáveis desconhecidos são `ELEVATED`, não `READ_ONLY`. `ShellApprovalGate` emite IDs aleatórios de uso único com expiração e fingerprint do comando, shell, cwd e timeout. A resposta textual do modelo não concede approval. Metadados limitados ficam na tabela `shell_executions`; auditoria redigida fica em `logs/shell.log`.

`RemoteCommandPolicy` reutiliza o classificador e acrescenta host/capability/action/resource. O OpenSSH usa host key estrita e somente aliases cadastrados. `AgentController` persiste resumos de runs com `turn_id`/`conversation_id`, detecta repetição/falhas, aplica runtime/tool/step limits, mantém locks durante ACT→VERIFY e retoma o mesmo run após approval. O `GroundingLedger` aceita stdout/stderr ou campos estruturados explicitamente projetados (`open`, `ready`, `healthy`, janelas sem título privado) como evidência; uma mutação só vira `VERIFIED` depois de um probe read-only correlato e positivo.

## Utamo Sentinel Bridge

O módulo `app.integrations.sentinel` mantém discovery, autenticação, Socket.IO, validação, dedupe e histórico separados do Network Watch. O Sentinel continua responsável por detectar alertas. A NYRA consome somente o schema público v1 e emite `SENTINEL_STATUS_CHANGED`, `SENTINEL_EVENT` e `SENTINEL_ALERT` no Event Bus. `ProactiveSentinelAlerts` decide UI/fala e usa a mesma Pronunciation Engine e SpeechQueue das conversas.

## Operator V2 (operador autônomo)

A camada 'app.operator/' acrescenta capabilities de operador autônomo sem criar um segundo cérebro: Screen Understanding (UIA-first, OCR fallback), App Adapters, Browser via CDP, clipboard local tipado (status metadata-only e write/clear verificados por Win32), Credential Broker (Credential Manager/DPAPI), sessões elevadas com TTL sobre UAC legítimo, jobs persistentes com reattach, Task Planner multi-step, Recovery Engine com rollback não-cego, Desktop Watcher event-driven (SetWinEventHook), Workflow Memory versionada, Proativo (default OFF) e contexts isolados (Task/Job/Watch/Workflow) com cross-context rejection. Watchdog externo independente vive em 'watchdog/nyra_watchdog.py'. Detalhes: docs/operator-v2.md.

## Autonomia local em sete camadas (NYRA-7c)

```text
ComputerPerceptionService -> ComputerStateService -> IntentResolver
          |                         |                      |
          +-------------------- EventBus -----------------+
                                                            v
ComputerPipeline -> Desktop/Operator capabilities -> EffectVerificationService
        |                                                   |
        +---------------- verified effects -----------------+
                                |
                   UsageLearningService -> SkillMemoryService
```

`app.computer` acrescenta contexto operacional e aprendizado ao operador existente sem criar um segundo executor. A percepção produz snapshots limitados de processos, janelas, arquivos recentes e metadados do clipboard; UIA é consultada sob demanda e pixels/OCR continuam sendo fallback. `ComputerStateService` mantém freshness por slot e referências naturais isoladas por conversa. Texto e voz convergem no mesmo `IntentResolver`, e `ComputerPipeline` só aceita o resultado estruturado do operador como evidência — texto livre da resposta nunca vira execução nem prova de sucesso.

Cada ação mutável continua pertencendo a uma capability tipada do Desktop/Operator, `system_shell` ou `remote_shell`, com as políticas e approvals já existentes. O clipboard comum usa `clipboard_write_text`/`clipboard_clear` (`LOW_RISK`); nenhuma resposta, evento ou Agent Run persiste seu conteúdo, e o campo é redigido antes do fingerprint persistente. Um efeito confirmado alimenta o estado, os eventos e o aprendizado de uso. Aliases e sequências só ganham confiança após efeitos verificados; correções negativas reduzem a associação errada. Skills aprendidas são estruturas versionadas com precondições, passos permitidos, verificação por passo, degradação e fallback. Estado de uso e skills fica local no perfil do operador e não armazena conteúdo do clipboard, áudio, tokens nem chain-of-thought.

## Self-Development Engine V1

`app.selfdev` permanece separado de identidade, LLM, memória, eventos, voz, ferramentas e UI. O fluxo é `RuntimeObserver → ImprovementDetector/Queue → SelfDevPlanner/RiskClassifier → WorktreeManager/CodeWorker → ValidationPipeline → PromotionManager → RestartValidator/RollbackManager`. O índice incremental persiste somente metadados e relações; código-fonte só é lido localmente durante planejamento. Toda execução de Git/test/build passa pelo `system_shell` e patches do modelo obedecem a um schema Pydantic, hashes e contenção de caminhos.

O repositório estável é a raiz canônica configurada, o workspace de candidatos fica fora dela e a publicação usa apenas um snapshot público separado. Estado mutável fica sob `%LOCALAPPDATA%\NYRA\selfdev`. `AUTONOMOUS_SAFE` promove apenas LOW_RISK; áreas de segurança/approval/credenciais/shell/publicação são HIGH_RISK. Auto Publish permanece OFF por padrão e nenhum texto gerado concede approval. Veja [self-development.md](self-development.md).
