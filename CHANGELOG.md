# Changelog

## 0.6.1 — 2026-09-05

### Verified

- TTS credential recognition after restart uses the protected vault, not a transient metadata index.
- The canonical fix was already included in the consolidated public v0.6.0 commit; its implementation, regression tests and migration note are byte-identical. No runtime fix is reapplied.

### Changed

- Public package and runtime version metadata advance to 0.6.1.

### Security

- No credentials, private runtime data or pending local VTube Studio changes are included.
- The v0.6.0 tag and historical release remain unchanged.

### Known Limitations

- Source-only distribution continues; prebuilt speech-binary redistribution notices remain under review.

## 0.6.0 — 2026-09-05

### Added

- Verified local migration of runtime data, configuration and project metadata.
- Compatibility for legacy environment variables, events, firmware protocols and protected credentials.

### Changed

- NYRA is now **Kazumi**: assistant identity, interface, wake-word default, package names, launchers and executables.
- Current documentation and repository links use the Kazumi name.

### Fixed

- Existing voice choices, custom wake words, memories and Open Loops survive the nominal migration.

### Migration

- See [the migration guide](docs/migration/nyra-to-kazumi.md) before upgrading an existing installation.
- The legacy Tauri application identifier remains stable for upgrade and WebView storage compatibility.
- Historical release names and persistence aliases are intentionally retained.

### Known Limitations

- Cloud STT/TTS requires separately configured provider credentials and may incur costs.
- Hardware effects and VTube Studio expressions require compatible local devices/models; third-party models and private data are not distributed.
- Customized external paths require explicit inventory and migration; conflicting data directories are never merged automatically.



## 0.5.0 — 2026-09-05

### Added

- Deepgram Nova-3 streaming STT, canonical interim/final transcripts and local Faster-Whisper fallback.
- Continuous Natural Conversation sessions, turn detection, barge-in, interrupted-turn tracking and Speech Planner.
- Universal TTS providers: Local/Kokoro, OpenAI, ElevenLabs, native Gradium and declarative Custom REST/WebSocket profiles.
- Grounded Hardware Engineering with project continuation, general code modification, bounded build repair and dynamic plan revision.
- World State, Open Loops/Goal Memory, Proactive Presence and Persona/Emotional Runtime integration.
- Network Observability V2 and VTube Studio-only presence with mouse tracking.

### Changed

- Natural Web Research preserves source provenance through the conversation/tool bridge and prioritizes specific official documentation.
- Public hardware project workspaces are configurable with `NYRA_PROJECTS_ROOT`; default is `<USER_HOME>/NYRA-Projects`.
- Public source snapshot excludes operator state and unfinished model-specific VTS changes.

### Fixed

- Decode bounded gzip/deflate responses before Web extraction; distinguish provider degradation from unavailable Internet.
- Correct HTTPS query validation, freshness routing and retrieved version excerpts.
- Preserve hardware grounding: user statements are not observed device state; build/upload is not proof of physical effect.
- Improve Voice settings readability and coordinated shutdown/cancellation lifecycle.

### Removed

- Obsolete internal-avatar fallback, old rig scaffolding and local validation screenshots from the current public source tree.

### Security

- Cloud credentials remain in Credential Broker, never configuration exports or release assets.
- TLS verification remains enabled. Custom TTS uses declarative contracts, not executable templates.
- Public release gates exclude private knowledge, projects, audio, credential stores, runtime databases and third-party VTube models.

### Known Limitations

- Cloud STT/TTS requires opt-in and provider credentials; provider charges may apply.
- Streaming/acoustic features depend on provider capabilities. Local fallback remains available.
- Physical flash, serial and effect validation require compatible hardware/toolchains; simulations are not physical proof.
- VTube expressions depend on the loaded model; model assets are not included.
- DuckDuckGo may return HTTP 202; Bing RSS and direct HTTPS provide fallback paths.
- See [release notes](docs/releases/0.5.0.md) for reproducibility and validation boundaries.

## 0.4.0 — 2026-08-30

### Added

- Intelligence Platform V2 com Memory V2, RAG local incremental, Context Engine, Model Router V2, Skills, Capability Registry, Autonomous Tasks, Event Intelligence, Diagnostics, Trace/Replay e Evaluation Suite.
- Browser Operator semântico, adapter opcional de visão local e telemetria real da plataforma na Operations UI.
- Resolução canônica de aplicativos, deduplicação de discovery, aliases, planos compostos e execução sequencial no Desktop Operator.
- Camada central de apresentação para respostas de aplicativos sem PID, HWND ou metadata interna no chat/TTS.
- Contexto recente de artefatos, apresentação de monitors, suporte USB e sincronização de presença/voz adicionados ao runtime existente.

### Changed

- SelfDev V2 preserva o lifecycle anterior e acrescenta reproduce, root-cause, static analysis, regression benchmark, canary e behavior comparison.
- O roteamento de modelos consulta o inventário Ollama real e degrada para fallbacks configurados.
- Configurações SelfDev agora usam caminhos relativos/portáveis; registries reais de rede ficam em arquivos `.local.*` ignorados.
- Versão unificada em backend, frontend, desktop/Tauri e API.

### Fixed

- Duplicatas técnicas de Start Menu, App Paths, PATH, Registry e AUMID deixam de produzir falsa ambiguidade quando representam o mesmo aplicativo.
- Operações determinísticas locais evitam Remote Shell e Agent Run desnecessários.
- Falhas de effect verification não geram respostas de sucesso.
- Respostas comuns de abrir, fechar, focar, minimizar, maximizar e restaurar deixam de expor detalhes internos.

### Security

- Conteúdo de web, documentos, tools e memória conserva trust boundary explícito e não substitui system policy.
- RAG aplica contenção de path e redaction antes da indexação; replay não repete ações destrutivas.
- Action Budget limita tool calls, retries, planner iterations, falhas, restarts e ações destrutivas/de rede.
- Configs reais de homelab e Trusted SSH foram removidas do material publicável e preservadas somente em arquivos locais ignorados.

### Known limitations

- Modelo Ollama vision continua opcional e pode ficar `UNCONFIGURED`.
- Integrações externas exigem configuração e disponibilidade do ambiente.
- Partes do SelfDev V2 e o cenário deliberado de loop infinito permanecem simulation-validated.
- A suíte completa conserva seis falhas ambientais/herdadas conhecidas: uma Home Assistant, uma fixture TTS, duas USB e duas VoiceHunter.

## 0.3.0 — 2026-08-26

- Adiciona bootstrap canônico idempotente (`npm run dev`/`npm start`), rebuild automático do sidecar PyInstaller antes do Tauri, release local regenerável e atalhos sem console para Área de Trabalho e Menu Iniciar.
- Adiciona o Self-Development Engine V1 modular: observação, fila persistente, mapa incremental do repositório, planejamento por evidência e classificação de risco.
- Implementa candidatos em Git worktrees, patches estruturados, seleção de testes, scan de secrets, regressão/benchmark, promoção com lock, pós-validação e rollback por `git revert`.
- Adiciona API e painel `Configurações > Self-Dev`, notificações, histórico, detalhe/diff e chip de status.
- Mantém o modelo local `qwen3:8b`, Auto Publish OFF no bootstrap e publicação restrita ao snapshot público sanitizado.
- Move estado mutável de desenvolvimento para `%LOCALAPPDATA%\NYRA` e documenta os diretórios canônicos em `E:`.
- Restaura os módulos de modelos/runtime supervisor que estavam ausentes da árvore canônica, preservando o checkpoint 7C existente.
- Endurece a API local com validação de Host/Origin/WebSocket e impede que transcrições de áudio concedam approvals.
- Remove lançamentos arbitrários de jobs/processos das superfícies LLM/REST e vincula mutações de runtime, energia, navegador e Home Assistant a approvals exatos de uso único.
- Torna mutações de homelab opt-in, exige `known_hosts` estrito no SSH e bloqueia credenciais fora de HTTPS ou de loopback HTTP literal.
- Impede ressurreição de tokens após desconexão, exige validação TLS do Proxmox e limita uploads de referência de voz a 50 MiB com leitura limitada.
- Fecha composições indiretas de sinks internos por tarefas/workflows/API, eleva sintaxe aninhada de shell e exige correspondência integral na auto-remediação SSH.
- Vincula approvals do Local Operator aos parâmetros materiais, reconhece confirmações destrutivas pelo contexto do modal e corrige fingerprints fail-closed de Credential Broker, recovery, visão e sessões elevadas.
- Invalida credenciais quando a origem Home Assistant/Proxmox muda e restringe o Sentinel a IP local literal, com HTTPS obrigatório fora de loopback.
