# Changelog

## 0.3.0 — 2026-08-27

- Adiciona bootstrap canônico idempotente (`npm run dev`/`npm start`), rebuild automático do sidecar PyInstaller antes do Tauri, release local regenerável e atalhos sem console para Área de Trabalho e Menu Iniciar.
- Adiciona as sete camadas do Universal Computer Operator: percepção, estado do computador, entendimento de intenção, operação universal, verificação de efeito, Usage Learning e Skill Memory.
- Torna o launcher de aplicativos universal, com fallback ordenado entre Menu Iniciar, executável, App Paths, AUMID/AppsFolder, ShellExecute e PATH/Get-Command, verificação real por PID/HWND e persistência do método comprovado no Application Registry.
- Adiciona o Self-Development Engine V1 modular: observação, fila persistente, mapa incremental do repositório, planejamento por evidência e classificação de risco.
- Implementa candidatos em Git worktrees, patches estruturados, seleção de testes, scan de secrets, regressão/benchmark, promoção com lock, pós-validação e rollback por `git revert`.
- Adiciona API e painel `Configurações > Self-Dev`, notificações, histórico, detalhe/diff e chip de status.
- Centraliza o transporte REST do frontend: navegador em desenvolvimento usa HTTP normal; a release Tauri usa uma ponte restrita ao backend local, preservando métodos, query, JSON, status e erros sem permitir destinos externos arbitrários.
- Corrige o canal de conversa/streaming na release Tauri e elimina chamadas diretas ao backend que causavam `Failed to fetch` e `cross_site_request` nos demais painéis.
- Sincroniza o cabeçalho com os endpoints reais de health, runtime, Watchdog e Self-Dev, incluindo polling, convergência após startup e distinção honesta entre modelo configurado e modelo ativo/residente.
- Estabiliza startup, launcher e reinício da release sem alterar o backend local-first ou as regras de approval.
- Mantém o modelo local `qwen3:8b`, o modo `AUTONOMOUS_SAFE`, Auto Publish OFF no bootstrap e publicação restrita ao snapshot público sanitizado.
- Move estado mutável de desenvolvimento para `%LOCALAPPDATA%\NYRA` e documenta os diretórios canônicos em `E:`.
- Restaura os módulos de modelos/runtime supervisor que estavam ausentes da árvore canônica, preservando o checkpoint 7C existente.
- Endurece a API local com validação de Host/Origin/WebSocket e impede que transcrições de áudio concedam approvals.
- Remove lançamentos arbitrários de jobs/processos das superfícies LLM/REST e vincula mutações de runtime, energia, navegador e Home Assistant a approvals exatos de uso único.
- Torna mutações de homelab opt-in, exige `known_hosts` estrito no SSH e bloqueia credenciais fora de HTTPS ou de loopback HTTP literal.
- Impede ressurreição de tokens após desconexão, exige validação TLS do Proxmox e limita uploads de referência de voz a 50 MiB com leitura limitada.
- Fecha composições indiretas de sinks internos por tarefas/workflows/API, eleva sintaxe aninhada de shell e exige correspondência integral na auto-remediação SSH.
- Vincula approvals do Local Operator aos parâmetros materiais, reconhece confirmações destrutivas pelo contexto do modal e corrige fingerprints fail-closed de Credential Broker, recovery, visão e sessões elevadas.
- Invalida credenciais quando a origem Home Assistant/Proxmox muda e restringe o Sentinel a IP local literal, com HTTPS obrigatório fora de loopback.
