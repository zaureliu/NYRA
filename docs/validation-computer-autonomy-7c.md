# Validação final — autonomia local em sete camadas (NYRA-7c)

Data: 2026-08-26
Status: **PASS**, com voz física marcada como `MANUAL_REQUIRED` e limitações ambientais documentadas abaixo.

## Retomada e preservação

A retomada começou pela inspeção de `git status`, `git diff`, arquivos alterados e artefatos em `.tmp`. As fases A–G já tinham implementação e testes direcionados; a primeira fase incompleta era a H (E2E cross-layer e integridade da UI). Nenhum `reset`, `clean`, `restore` ou descarte de arquivo untracked foi usado. O E2E da fase H revelou lacunas reais de integração nas camadas anteriores; elas foram corrigidas incrementalmente, preservando o trabalho existente.

## Checkpoints obrigatórios

### FASE A — Perception

- Status: PASS.
- Arquivos: `backend/app/computer/perception.py`, composição em `backend/app/main.py` e eventos no EventBus existente.
- Testes: snapshots limitados, eventos de janela/processo/aplicação/arquivo/clipboard, debounce, ausência de tempestade inicial e consumo pelo Computer State.
- E2E: abertura, foreground e fechamento do Notepad foram observados no runtime real; o estado foi atualizado.
- Issues: o primeiro snapshot gerava risco de eventos de arquivo antigos e o callback de estado não estava conectado. Corrigidos.

### FASE B — Computer State

- Status: PASS.
- Arquivos: `backend/app/computer/state.py` e endpoint `GET /api/computer/state`.
- Testes: freshness `FRESH/STALE/UNKNOWN`, relógio injetado, refresh por percepção, persistência mínima, resolução de referências e isolamento por conversa.
- E2E: `ele` resolveu para o Notepad aberto; `last_target` permaneceu coerente após minimizar/restaurar/fechar.
- Issues: um alvo observado em outra conversa podia contaminar a resolução anterior. Corrigido com escopo por `conversation_id` e fallback explícito de foreground.

### FASE C — Intent Understanding

- Status: PASS.
- Arquivos: `backend/app/computer/intent.py` e `backend/app/desktop/intents.py`.
- Testes: normalização, referência, fast path, texto/voz convergentes, compound intent, aspas retas/curvas e `close_after` opcional.
- E2E: cadeia `abre -> minimiza ele -> traz ele de volta -> fecha ele` e comando composto do Notepad.
- Issues: o parser composto sempre fechava o Notepad, mesmo sem pedido, e não aceitava todas as aspas. Corrigido.

### FASE D — Universal Operator integration

- Status: PASS.
- Arquivos: `backend/app/computer/pipeline.py`, `backend/app/desktop/control.py`, `backend/app/desktop/multistep.py`, `backend/app/operator/clipboard.py`, registro em `backend/app/operator/tools_reg.py` e preflight em `backend/app/api/routes.py`.
- Testes: seleção de capability, owner único, operação dinâmica, contexto, dedupe, multi-step e modo offline do LLM.
- E2E: ações reais Win32 e skill executadas com Ollama indisponível, provando que o fast path determinístico não depende de nuvem/LLM.
- Issues: `last_operation_result` podia reter evidência de uma ação anterior; resposta de operação explícita podia usar um label antigo. Corrigidos. A API agora consulta o pipeline local antes de recusar chat por LLM offline.

### FASE E — Effect Verification

- Status: PASS.
- Arquivos: `backend/app/computer/verification.py`, integração no pipeline e multi-step.
- Testes: janela, app, arquivo/conteúdo, processo, browser sem sessão, conversão de resultado estruturado e `UNKNOWN` honesto.
- E2E: minimizar/restaurar/fechar por HWND, salvar conteúdo e confirmar fechamento. Target inexistente retornou `NOT_FOUND` e “Nada foi executado”.
- Issues: sucesso do PLAN ainda era inferido da frase da resposta. Corrigido; somente `effect_verified`/evidência estruturada produz `VerifiedEffect` positivo.

### FASE F — Usage Learning

- Status: PASS.
- Arquivos: `backend/app/computer/usage.py` e integração no pipeline.
- Testes: agregação, aliases após threshold, preferências, janela deslizante de workflow, correção negativa, persistência e redaction.
- E2E: o padrão repetido criou o workflow candidate a partir de `0.60`; nas repetições finais ele chegou a `0.95`. O alias `vscode -> Visual Studio Code` acumulou 13 sucessos e confiança `0.99`, sempre após efeitos reais.
- Issues: pares não sobrepostos fragmentavam sequências e uma associação errada de alias mantinha votos positivos após correção. Corrigidos com janela deslizante e redução explícita da associação anterior.

### FASE G — Skill Memory

- Status: PASS.
- Arquivos: `backend/app/computer/skills_memory.py` e endpoints locais de skills.
- Testes: candidate, promoção, match, precondições, steps permitidos, exigência de evidência, versionamento, degradação e capability desconhecida fail-closed.
- E2E: candidate derivada de ações verificadas, skill promovida disparada por linguagem natural e falha de precondição sem alegação de sucesso, com redução de confiança.
- Issues: steps de workflow eram armazenados como alvo completo (`OPEN_APP:code`) e ausência de evidência podia ser tratada como sucesso. Corrigidos.

### FASE H — Cross-layer E2E + UI integrity

- Status: PASS.
- Arquivos: `scripts/e2e_computer_layers_runtime.py`, `scripts/e2e_frozen_parity_7c.py` e `frontend/scripts/ops-ui-smoke.mjs`.
- Testes/E2E: runtime-fonte `13/13 PASS`; executável PyInstaller atual `8/8 PASS`; smoke geral da UI PASS; smoke de operações percorreu 13 rotas PASS.
- Issues: o harness inicial não detectava uma janela residual, não falhava o processo quando havia cenário FAIL e usava espera fixa curta na página Capabilities. A primeira repetição congelada também criou a fixture fora das raízes conhecidas pelo resolvedor. Corrigidos com baseline de HWND, cleanup seletivo, exit code obrigatório, polling limitado, fixture exclusiva em `Documents` e armazenamento `NYRA_*` isolado no runtime temporário.
- Telemetria futura: os sete sinais passivos do §103 (`perception_failure`, `intent_resolution_failure`, `context_resolution_failure`, `operator_failure`, `verification_failure`, `usage_pattern_failure`, `skill_execution_failure`) têm contadores e eventos redigidos; não executam SelfDev.

## PERCEPTION

- Processes: PASS — PID, nome, executável, parent, start time, CPU/memória e relação com foreground, limitados aos processos com janela observada.
- Windows: PASS — mapa Win32 com HWND, PID, título, classe, visibilidade, bounds e estados minimized/maximized/foreground.
- UIA: PASS para adapter on-demand e testes nativos; não é varrida continuamente.
- Filesystem: PASS — arquivos recentes limitados às pastas comuns, cacheados, com eventos de criação/modificação sem tempestade no bootstrap.
- Clipboard: PASS — somente tipo, tamanho e mudança; conteúdo não é lido pela percepção.
- Browser: DEGRADED honesto quando não existe sessão CDP gerenciada; testes do adapter de browser passaram nativamente.
- Network/Homelab: PASS para integração de summary/status local e read-only, com timeout/cooldown preservados.
- Vision: disponível somente sob demanda como fallback; suíte do operador passou.
- OCR fallback: baixa confiança, nunca promovido sozinho a fato; não foi necessário no E2E crítico.
- EventBus: PASS — eventos normalizados, debounce, fila limitada e publicação assíncrona sem bloquear chat.

## COMPUTER STATE

- foreground: atualizado por snapshot da percepção.
- context slots: `foreground_app`, `open_apps`, `last_target*`, `last_successful_action`, `last_foreground_window` e contexto de arquivo/pasta.
- freshness: PASS com TTL e estados `FRESH`, `STALE`, `UNKNOWN` usando relógio injetável.
- turn isolation: PASS; alvos contextuais ficam separados por conversa.
- world state integration: summaries de browser, homelab, network e atividade do usuário são compactos e lazy.

## INTENT

- text normalization: PASS.
- voice normalization: PASS por teste de convergência no mesmo resolver.
- references: PASS para `ele`, `ela`, `isso`, app/pasta/arquivo e foreground.
- fast path: PASS inclusive com Ollama offline.
- compound: PASS; `close_after` segue exatamente o pedido.

## UNIVERSAL OPERATOR

- FULL_LOCAL_OPERATOR: PASS quando ativado; `read_only=false` foi confirmado no runtime-fonte e no congelado.
- clipboard: `clipboard_status` é `READ_ONLY`; `clipboard_write_text` e `clipboard_clear` são `LOW_RISK`, têm schemas Pydantic e efeito Win32 verificado. Conteúdo não é lido pela percepção, ecoado pela tool nem persistido em logs/eventos/Agent Run; o campo é redigido antes do fingerprint persistente.
- tool selection: intents determinísticos escolhem capabilities existentes; shell não é inferido de texto livre.
- one action owner: `ComputerPipeline` orquestra; Desktop/Operator continua sendo o executor real.
- dedup: locks/resultados existentes são reutilizados, sem segundo executor.
- multi-step: PASS para abrir, escrever, salvar, verificar conteúdo e fechar.
- recovery: falhas retornam código/estágio recuperável; target/precondição ausente não executa fallback perigoso.

## EFFECT VERIFICATION

- apps/windows: PASS por Win32 e resultado estruturado do operador.
- files: PASS por existência, tamanho/mtime e conteúdo quando solicitado.
- process/services: PASS para psutil; serviços continuam nas capabilities tipadas existentes.
- browser: `UNKNOWN` honesto sem sessão; adapter CDP coberto por testes.
- homelab: continua read-only por padrão e exige fonte do adapter.
- false success claims: 0 nos cenários críticos; texto da resposta não é usado como prova.

## USAGE LEARNING

- events: somente ações concluídas e efeitos verificados alimentam sucesso.
- aliases: threshold/confiança e correção negativa confirmados.
- preferences: agregadas sem conteúdo privado.
- workflow candidates: janela deslizante, sequência real e confidence growth confirmados.
- negative corrections: associação errada perde votos; associação corrigida recebe confirmação explícita.
- storage: `%LOCALAPPDATA%/NYRA/usage-learning`, com escrita atômica.
- privacy: sem áudio, clipboard content, tokens, secrets ou chain-of-thought.

## SKILL MEMORY

- candidate: criada apenas de workflow confirmado ou pedido explícito.
- promotion: explícita e auditável; não há autoexecução silenciosa.
- execution: match -> preconditions -> step permitido -> efeito verificado -> próximo step.
- failure handling: fail-closed, confidence reduction, degraded e fallback ao planner.
- versioning: versão e histórico preservados.
- storage: `%LOCALAPPDATA%/NYRA/skills`, local e sem secrets.

## UI

- Integrity: PASS.
- Bugs found: 0 no produto; 1 defeito de timing no smoke.
- Bugs fixed: 0 no produto; 1 no harness de smoke.
- Non-essential changes: 0. Nenhum componente, CSS, rota ou layout foi alterado pela feature; somente o harness ganhou polling limitado.
- Redesign: NO.
- Baseline técnico: rotas/componentes/testes/build registrados antes da fase H em `.tmp/ui-baseline-hash.txt`.

## E2E

| Scenario | Result | Effect Verified | Evidence |
|---|---:|---:|---|
| Abrir Notepad | PASS | Sim | janela real + PID/HWND |
| Minimizar `ele` | PASS | Sim | estado `iconic=true` |
| Trazer `ele` de volta | PASS | Sim | janela restaurada/foreground |
| Fechar `ele` | PASS | Sim | janela ausente após close |
| Compound Notepad: escrever/salvar/fechar | PASS | Sim | conteúdo do arquivo + janela fechada; fixture removida |
| Usage/workflow/alias | PASS | Sim | workflow `0.60 -> 0.95`; alias 13 sucessos/`0.99` |
| Skill candidate e execução natural | PASS | Sim | steps limpos e todos verificados |
| Skill com precondição quebrada | PASS | Sim (falha) | nada executado + confidence degradada |
| Target inexistente | PASS | Sim (ausência) | `NOT_FOUND`; nada executado |
| FULL_LOCAL_OPERATOR | PASS | Sim | health `read_only=false` |
| Clipboard comum em FULL_LOCAL_OPERATOR | PASS | Controlado | tools tipadas com riscos esperados; backend injetável confirmou write/clear; clipboard real não foi lido nem alterado |
| Runtime-fonte | 13/13 PASS | Sim | `.tmp/nyra-7c-runtime-e2e.json` |
| Runtime congelado atual | 8/8 PASS | Sim | `.tmp/nyra-7c-frozen-e2e.json` |
| Texto/voz normalizados | PASS / MANUAL_REQUIRED | Unitário | mesma intenção no teste; microfone físico indisponível |
| UI navigation | PASS | N/A | smoke geral + 13 rotas de operações |

## TESTS

- backend específico 7c: `38 passed`.
- Operator V2 API nativo: `9 passed` (1 warning de depreciação Starlette; warnings conhecidos do Proactor no teardown).
- regressão ampla final de camadas/EventBus/API/Agent Loop/conversa/realtime/turn isolation/operadores: `178 passed` (7 warnings conhecidos de transport async/Starlette no teardown).
- pós-endurecimento do fingerprint privado: `38 passed` na suíte 7c e `48 passed` em Agent Loop/grounding.
- backend targeted 7c/operator/API/EventBus: `113 passed` (3 warnings de transport assíncrono preexistentes).
- backend computer/operator: `107 passed`.
- backend computer/API: `35 passed`.
- backend completo no sandbox: `670 passed, 1 skipped, 22 blocked` por Win32/DPAPI/CDP/captura/controle de processo. Repetição nativa dos sete arquivos afetados: `15 passed` + `72 passed`; todos os 22 casos antes bloqueados passaram, sem falha de asserção.
- frontend Vitest: 24 arquivos, `102 passed`.
- frontend build: TypeScript + Vite PASS.
- frontend UI smoke: PASS; Operations UI smoke: 13/13 rotas PASS.
- desktop Rust tests: `7 passed`; release build PASS (somente warnings preexistentes de imports/dead code).
- frozen PyInstaller build: PASS em diretório temporário isolado. Artefato atual: `25.105.000` bytes, SHA-256 `A9FDEA65AFA49C3BD5DADD590099F174E39F5677CFC27B975391F1FD60142106`.
- voice: normalização compartilhada PASS; captura física `MANUAL_REQUIRED`.
- `git diff --check`: PASS (exit 0; apenas avisos de conversão LF/CRLF do Git).

## LIMITATIONS

- O Ollama estava offline durante os E2E; o health ficou `degraded`. Os fluxos locais determinísticos passaram e essa condição comprovou o fast path, mas geração livre por LLM não foi revalidada nesta rodada.
- Não havia microfone físico disponível no health; por isso voz real não recebeu PASS inventado e permanece `MANUAL_REQUIRED`.
- Não havia sessão CDP gerenciada durante o E2E crítico. Browser retornou indisponibilidade honesta; o adapter foi validado pela suíte nativa.
- Vision/OCR não foram necessários nos cenários críticos; permanecem fallback on-demand e foram cobertos pela suíte do operador.
- O E2E não substituiu nem leu o clipboard real do operador. A mutação foi validada com backend Win32-injetável controlado; runtime-fonte e congelado confirmaram a superfície tipada e ausência de bloqueio `AGENT_READ_ONLY`.
- O full pytest precisa de execução fora do sandbox para casos que controlam Win32, DPAPI, CDP, screenshot/UIA e processos. Esses casos passaram na repetição nativa isolada.

## Critério final

O pipeline está funcional e observado em runtime real e congelado:

```text
PERCEPTION -> COMPUTER STATE -> INTENT -> OPERATOR -> VERIFICATION
           -> USAGE LEARNING -> SKILL MEMORY
```

No checkpoint 7C original ainda não haviam sido implementados Self-Development Engine, autoedição, autopromoção de patches, release 0.3.0, publicação no GitHub ou movimentação do repositório. A implementação posterior e seus gates estão documentados em `docs/validation-self-development-v1.md`; esta evidência histórica permanece preservada.
