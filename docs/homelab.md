# Homelab e segurança operacional

O registry preserva tools estruturadas `READ_ONLY` e agora expõe `system_shell` por schema nativo do Ollama. Nomes inexistentes são rejeitados e texto livre nunca é executado. O comando da tool passa por classificação dinâmica, approval vinculado quando sensível, timeout, limite de saída, redaction e auditoria.

O registry local configurado (por padrão, `config/network_aliases.local.json`) é a fonte do Trusted Host Registry para `remote_shell`. Somente hosts com `remote_shell.enabled=true` podem receber SSH, e o modelo nunca escolhe address/user/port/key. Capabilities de Proxmox/OpenWrt e allowlists de remediação são validadas no backend.

Ferramentas estruturadas: ping, DNS, conexão TCP, métricas locais, interfaces e HTTP/HTTPS. Elas permanecem preferíveis quando fornecem dados validados. Diagnósticos ad hoc podem usar PowerShell/CMD real via `system_shell`. Uso geral entra em `logs/tools.log`; comandos locais também entram em `logs/shell.log` e no histórico limitado `shell_executions`.

O monitor coleta métricas locais a cada 60 segundos. CPU e RAM acima de 90% geram evento e memória de homelab, com cooldown de 15 minutos. `PROACTIVE_MODE=false` evita comentários espontâneos no MVP.

Proxmox aceita token de API via `.env` e oferece apenas GET de nodes, VMs e storage. Recomenda-se usuário/token dedicado com privilégio de auditoria. OpenWrt possui uma interface de transporte e allowlist; API/ubus/SSH ainda não são ativados.

Níveis do shell:

- `READ_ONLY`: inspeção autônoma.
- `LOW_RISK`: pequena mudança reversível, autônoma.
- `ELEVATED`: mudança sensível; o backend exige approval no fluxo conservador atual.
- `DESTRUCTIVE`: sempre exige confirmação explícita vinculada.
- `CRITICAL`: nunca é inferido como autorizado; exige confirmação específica vinculada.
