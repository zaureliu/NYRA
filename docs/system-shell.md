# System Shell local

`system_shell` fornece execução local arbitrária de PowerShell e CMD ao agente NYRA. A tool está no mesmo `ToolRegistry` usado pelo backend, recebe schema Pydantic no `/api/chat` do Ollama e devolve stdout, stderr, exit code, duração, timeout, truncamento e risco. Texto comum do modelo nunca é avaliado como comando.

## Fluxo

1. `ToolAgentLoop` envia ao Ollama apenas tools habilitadas e limita chamadas consecutivas por turno.
2. O modelo chama `system_shell` com comando, shell/cwd/timeout opcionais e uma razão curta.
3. `ShellRiskClassifier` analisa todos os componentes encadeados e pipelines. Regras cobrem cmdlets, executáveis, aliases, argumentos, redirecionamento, scripts, filesystem, Git, Docker, serviços, rede, registry, segurança, boot e storage. Comando desconhecido sobe para `ELEVATED`.
4. `READ_ONLY` e `LOW_RISK` executam. `ELEVATED`, `DESTRUCTIVE` e `CRITICAL` exigem approval no fluxo conservador atual.
5. `ShellApprovalGate` emite um `approval_id` aleatório, expira em cinco minutos e o vincula por SHA-256 ao comando exato, shell, cwd e timeout. O backend só concede pelo endpoint explícito ou por uma resposta conversacional estrita do operador quando há exatamente uma pendência. O ID é consumido uma vez.
6. `ShellExecutor` usa `pwsh.exe` quando disponível e cai para `powershell.exe`; CMD usa `cmd.exe /D /S /C`. Não existe bypass de UAC. SSH é um subsistema separado, `remote_shell`, limitado ao Trusted Host Registry.
7. Saída é decodificada com UTF-8/code page Windows e erros substituíveis, redigida e truncada preservando início e fim. No timeout, a árvore do processo é encerrada.
8. O resultado retorna ao modelo como mensagem `tool`; somente a resposta natural final entra na memória curta e no TTS.

## Configuração

- `NYRA_SHELL_ENABLED=true`
- `NYRA_SHELL_DEFAULT=powershell`
- `NYRA_SHELL_TIMEOUT_SECONDS=30`
- `NYRA_SHELL_MAX_TIMEOUT_SECONDS=300`
- `NYRA_SHELL_MAX_OUTPUT_CHARS=50000`
- `NYRA_SHELL_MAX_CALLS_PER_TURN=10`
- `NYRA_SHELL_CONFIRM_DESTRUCTIVE=true`
- `NYRA_SHELL_APPROVAL_TTL_SECONDS=300`
- `NYRA_SHELL_DEFAULT_WORKING_DIRECTORY=.`

Quando desabilitada, a tool permanece acessível pela API para devolver `SHELL_DISABLED`, mas não é anunciada ao LLM.
Se `NYRA_SHELL_CONFIRM_DESTRUCTIVE=false`, comandos sensíveis ficam bloqueados com `COMMAND_REJECTED`; a opção nunca transforma ausência de confirmação em execução automática.

## API e observabilidade

- `GET /api/shell/status`
- `GET /api/shell/history?limit=50`
- `GET /api/shell/approvals`
- `POST /api/shell/approvals/{approval_id}` com `{ "approved": true|false }`
- `POST /api/tools/system_shell` com os parâmetros da tool

Eventos `SHELL_EXECUTION_STARTED`, `SHELL_EXECUTION_FINISHED`, `SHELL_APPROVAL_REQUIRED` e `SHELL_APPROVAL_DECIDED` alimentam WebSocket, dashboard e Desktop Presence. Auditoria JSON rotativa usa `logs/shell.log`. A tabela `shell_executions` armazena apenas metadados e comando redigido, nunca stdout/stderr completo.

## Limitações deliberadas

- Comandos são finitos; não há gerenciador de daemons nesta versão.
- Elevação UAC não é automatizada. Falha por privilégios volta ao modelo.
- SSH controlado existe somente via `remote_shell`; WinRM e PsExec permanecem fora do escopo.
- Heurísticas não equivalem a um parser completo de PowerShell. Por isso, execução indireta, script blocks de controle e executáveis desconhecidos são classificados conservadoramente.
