# Segurança

## Fronteiras obrigatórias

- Texto livre do LLM nunca é executado. Comandos locais passam por `system_shell`; SSH passa por `remote_shell` e por um host lógico cadastrado.
- Executáveis desconhecidos não são presumidos seguros. Criação arbitrária de jobs e processos não é exposta à API nem ao LLM.
- Approvals são explícitos, de uso único e vinculados ao comando/payload, recurso, risco e contexto. Respostas do LLM, uploads de áudio e Always Listening não concedem approval.
- SSH com chave ou senha exige `known_hosts` pré-provisionado. A primeira chave vista não é aceita nem gravada automaticamente.
- Mutações de homelab ficam desativadas por padrão. Quando o operador as habilita, cada mutação ainda exige approval e verificação read-only posterior.
- Substituições e avaliações aninhadas de PowerShell/Bash são classificadas no mínimo como `ELEVATED`; remediation SSH automática só reconhece comandos inteiros de uma allowlist, nunca prefixos com sufixos arbitrários.

## API local e navegador

O backend escuta em loopback e aplica uma barreira ASGI antes das rotas. `Host` deve ser `127.0.0.1`, `localhost` ou `::1`; origens de navegador devem corresponder exatamente às origens de desenvolvimento/Tauri autorizadas. Requisições `cross-site` e WebSockets com origem hostil são rejeitados. Clientes nativos/CLI sem `Origin` continuam aceitos.

Todo JavaScript arbitrário no navegador exige approval. O fingerprint inclui o hash do script, a aba resolvida e o hash da URL completa (query e fragment incluídos). A execução usa uma guarda síncrona no mesmo contexto JavaScript para recusar navegação ocorrida entre approval e avaliação.

## Credenciais e integrações

Credenciais configuradas pela interface vivem no Credential Broker do Windows. Excluir/desconectar exige approval e grava um tombstone que impede fallback silencioso para valores legados de `.env` ou arquivos antigos. Tokens de Home Assistant e pares de API Token do Proxmox são vinculados à origem configurada: mudar scheme, host ou porta invalida a credencial anterior, salvo quando um novo par Proxmox é fornecido atomicamente na mesma atualização.

Tokens HTTP só podem ser enviados a um IP loopback literal. Home Assistant, Sentinel e Proxmox exigem HTTPS fora de loopback. O Sentinel aceita somente IP local literal no host manual. O Proxmox exige validação TLS ativa; para certificados internos, a CA deve ser instalada no repositório de confiança do host KAZUMI.

Secrets, `.env`, bancos, áudio privado, logs, modelos e estado de runtime são ignorados pelo Git e não entram no snapshot público.

## Self-Development Engine

O SelfDev usa worktrees isolados e patches estruturados com paths contidos, hash de base, limites e scan antes dos testes. Segurança, approval, credenciais, redaction, shell, host keys e publicação são áreas HIGH_RISK e nunca são autopromovidas. Promoção usa commit/cherry-pick sob lock; rollback usa `git revert`, sem rewrite de histórico.

## Limite conhecido

A API é destinada ao operador local e ainda não usa uma credencial de sessão por lançamento. A barreira de Host/Origin reduz ataques de navegador, mas um processo já executando como o mesmo usuário local continua dentro da fronteira de confiança do host. Não exponha a porta backend na LAN e não use proxy que remova ou reescreva essas validações.

Para relatar uma vulnerabilidade, não publique credenciais, bancos, áudio ou logs privados em issues públicas. Envie somente uma reprodução mínima e sanitizada ao mantenedor do repositório.

## Fronteiras de confiança V2

Contexto recebe um rótulo obrigatório: `SYSTEM_TRUSTED`, `USER_INPUT`, `TOOL_TRUSTED`, `TOOL_UNTRUSTED`, `REMOTE_CONTENT`, `WEB_CONTENT`, `DOCUMENT_CONTENT` ou `MEMORY_CONTENT`. Web, documentos, memória e resultados remotos são dados sem autoridade de instrução, mesmo quando contêm frases como “ignore as instruções anteriores”. O Context Engine serializa essa fronteira; Browser Operator e RAG também a preservam no resultado estruturado.

Memory V2 recusa material com aparência de credencial e não substitui o Credential Broker. RAG restringe ingestão a roots autorizados, resolve o caminho antes da leitura e preserva provenance. Replay é dry-run seguro: chamadas de tool não são reexecutadas. Action Budget limita tools, retries, planner, falhas, tempo, restarts e ações destrutivas/rede. Traces, eventos e diagnósticos passam pela redaction estruturada antes da persistência ou exposição na API.

Open Loops persistem somente contexto resumido e referências de artefatos; candidatos com secrets são rejeitados ou redigidos. Um loop não concede approval nem executa ação. `RESOLVED` exige evidência estruturada de uma autoridade local, e fontes `llm`, `assistant`, `model` ou `prompt` são recusadas como prova de resolução.

O Proactive Presence aceita apenas eventos estruturados de fontes internas
registradas, redige mensagem e entidade antes da persistência e nunca chama
tools operacionais. Toda notificação carrega `execution_authorized=false` e
`action_budget_consumed=0`; responder “continua” apenas recupera contexto e
continua sujeito a Grounding, Action Budget, Credential Broker, risk policy e
Approval Gate. Voz proativa é opt-in e é suprimida enquanto usuário ou
assistente estão falando/ouvindo.
