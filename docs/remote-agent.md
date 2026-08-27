# Trusted SSH e Agent Runs

`remote_shell` executa comandos SSH finitos somente em hosts presentes em `config/network_aliases.json`. O modelo fornece apenas `host` lógico, `command`, timeout/cwd opcionais, razão e eventual `approval_id`. Address, port, username, private key, known_hosts e SSH agent são resolvidos no backend e nunca entram no schema do LLM.

## Confiança e autenticação

- O cliente é o OpenSSH nativo, em `BatchMode=yes` e `StrictHostKeyChecking=yes`.
- Password interativo não é suportado. Use chave, preferencialmente ed25519, ou SSH agent autorizado.
- `known_hosts` é obrigatório. Ausência ou mudança inesperada bloqueia a conexão; não há fallback para `StrictHostKeyChecking=no`.
- IP direto no input da tool é rejeitado, mesmo que corresponda a um host cadastrado.
- Cada host declara platform, capabilities, enable flag, auto-remediation actions normalizadas e recursos gerenciados.

Baseline:

- `gateway`/`openwrt`: diagnostics, network, logs e service_management.
- `proxmox`: diagnostics, network, logs, service_management, containers, virtualization e storage.
- `dc1`: registro de rede presente, SSH desabilitado.

Os usuários e caminhos de autenticação são configuração local. Ajuste o registry para corresponder às contas/chaves reais; nunca versione uma private key.

## Risco e approval

O mesmo `ShellRiskClassifier` analisa comandos remotos com regras Linux/OpenWrt/Proxmox. A policy acrescenta capability, ação normalizada e recurso. Mudanças remotas são conservadoras; `DESTRUCTIVE` e `CRITICAL` nunca são auto-remediation.

Um approval é associado por SHA-256 a comando, host-alvo, cwd, timeout e `agent_run_id`, expira e só pode ser consumido uma vez. Auto-remediation exige simultaneamente:

1. `NYRA_AGENT_AUTO_REMEDIATION=true`;
2. action ID na allowlist global;
3. action ID na allowlist do host;
4. recurso exato no `managed_resources` do host.

As allowlists vêm vazias no baseline, portanto nenhuma mutação remota é automática após instalação.

## Agent Controller

`AgentController` cria `run_<uuid>` e coordena o `ToolAgentLoop` em OBSERVE, DIAGNOSE, PLAN, ACT, VERIFY e COMPLETE/FAILED/WAITING_APPROVAL/CANCELLED. Ele pode combinar system_shell, remote_shell, Network Watch e Sentinel no mesmo contexto.

Persistem somente objetivo, estados, tools, operações redigidas, fingerprints, resumos limitados e status. Outputs integrais e chain-of-thought não são persistidos.

Proteções:

- limites de steps, tool calls e runtime total;
- contador de falhas consecutivas;
- fingerprint de comando + resultado para detectar ausência de progresso;
- lock por recurso durante mutação e verificação;
- verificação READ_ONLY obrigatória após mudança;
- cancelamento por voz/chat e API, com encerramento de subprocesso SSH/local controlado quando possível;
- `NYRA_AGENT_READ_ONLY=true` bloqueia mutações dentro do loop.

## API e observabilidade

- `GET /api/remote-shell/status`
- `GET /api/remote-shell/history`
- `POST /api/tools/remote_shell`
- `GET /api/agent/status`
- `GET /api/agent/runs`
- `GET /api/agent/runs/{run_id}`
- `POST /api/agent/runs/{run_id}/cancel`

Auditoria rotativa: `logs/remote-shell.log` e `logs/agent-runs.log`. Metadados ficam em `remote_executions` e `agent_runs` no SQLite.

## Limitações

- Não há sessões interativas, password prompt, port forwarding, SFTP, SSH arbitrário, WinRM ou PsExec.
- Não há pooling permanente; cada comando fecha sua sessão. Isso reduz complexidade e credenciais ociosas.
- O parser de risco é heurístico e fail-closed para comandos desconhecidos.
