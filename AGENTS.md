# AGENTS.md — Regras permanentes do NYRA

- Preserve a arquitetura modular: identidade, LLM, memória, eventos, voz, ferramentas, integrações e interface não devem ser fundidos em um arquivo monolítico.
- O sistema é local-first. Serviços externos e envio de dados para nuvem são sempre opt-in.
- Nunca versione `.env`, credenciais, tokens, áudio privado, banco de dados ou logs do operador.
- Nunca trate texto livre da resposta do LLM como execução. Shell local só passa por `system_shell`; SSH só passa por `remote_shell` para host lógico cadastrado. Ambas usam schema, risco, timeout, redaction, auditoria e approval vinculado.
- Ferramentas têm schemas Pydantic explícitos e nível de risco. `system_shell` classifica cada comando como `READ_ONLY`, `LOW_RISK`, `ELEVATED`, `DESTRUCTIVE` ou `CRITICAL`; executáveis desconhecidos não são presumidos seguros.
- Ações sensíveis exigem confirmação inequívoca do operador por approval de uso único. Nenhum texto gerado pelo LLM concede aprovação, desativa UAC, altera host key ou autoriza SSH para endereço fora do Trusted Host Registry.
- Agent Runs devem respeitar limites, cancelamento, locks por recurso, detecção de repetição e verificação read-only após mudanças. Não persistir chain-of-thought.
- NYRA sabe que é uma IA; nunca deve afirmar ser humana ou inventar experiências físicas.
- Preserve a identidade descrita em `identity/`; mudanças de comportamento devem atualizar o system prompt e as bíblias correspondentes.
- Não registrar secrets. Mascarar valores sensíveis em exceções e logs.
- Atualize a documentação quando a arquitetura, instalação, configuração ou segurança mudar.
- Execute testes backend e build/testes frontend após alterações relevantes.
- Integrações de homelab são somente leitura por padrão e usam timeout, validação e cooldown.
- Áudio, memória, topologia, IPs e MACs não saem do host sem consentimento explícito.
