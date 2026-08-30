# Changelog

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
