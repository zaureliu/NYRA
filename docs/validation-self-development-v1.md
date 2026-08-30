# Validação — Self-Development Engine V1

Data: 2026-08-26. Candidato: `<SELFDEV_WORKSPACE>/worktrees/SELFDEV-0001`.

## Gates concluídos

- Integração backend inicial: 35 testes passaram.
- API/config/eventos após endpoints: 9 testes passaram.
- Suíte dedicada SelfDev: 9 testes passaram, incluindo API real e bloqueio HIGH_RISK antes do worktree.
- Frontend Vitest: 24 arquivos, 102 testes passaram.
- Frontend TypeScript/Vite: build de produção passou.
- npm audit do conjunto instalado: zero vulnerabilidades reportadas.

Os testes SelfDev cobrem índice incremental, consulta de símbolo/rota, deduplicação e threshold de evidência, persistência da fila, classificação de áreas protegidas, contenção/hash/secrets do patch, scan antes da execução, allowlist de comandos, benchmark, árvore estável suja, cherry-pick e rollback por `git revert`.

## Retomada e hardening (2026-08-27)

- Suíte backend integral pós-hardening: **748 passed** (19 warnings conhecidos de teardown/depreciação, zero falhas).
- Frontend Vitest: **24 arquivos / 102 testes passed**.
- Frontend TypeScript/Vite: **PASS**.
- Smoke da interface contra o artefato de produção (`vite preview`): **PASS**.
- Browser Control V2 real (Chromium/CDP local): **6 passed**.
- `git diff --check`: **PASS**.
- Uma auditoria de segurança antes da correção encontrou caminhos de bypass em API local, approvals, processos, browser, SSH, homelab e transporte de credenciais. Os achados foram corrigidos e receberam testes de regressão específicos.

Os gates de Tauri, runtime canônico promovido, scan pós-correção, sincronização pública e publicação são registrados ao final da retomada. Nenhuma publicação parcial é autorizada por este documento.
