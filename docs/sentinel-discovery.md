# Sentinel Discovery

O discovery é HTTP leve e valida `GET /api/integrations/nyra/health`. Uma porta aberta não basta: `service=utamo-sentinel`, `integration=nyra`, versão do protocolo, `instance_id` e capabilities precisam validar pelo schema local.

Ordem:

1. IP local literal manual quando preferido;
2. último host confirmado em `data/sentinel/last-known.json`;
3. `127.0.0.1` literal;
4. host manual não prioritário;
5. somente então redes IPv4 privadas explicitamente allowlisted.

Hostnames são recusados para eliminar resolução ambígua/rebinding. HTTP é aceito somente para loopback literal; IPs privados ou link-local usam HTTPS. Um `last-known.json` legado com HTTP/LAN é promovido para HTTPS antes do probe, e valores não locais são descartados.

Allowlist aceita no máximo `/24` por entrada, até oito redes, concorrência máxima 8 e timeout curto. Redes públicas, IPv6 de varredura e intervalos maiores são rejeitados. Não há Nmap, SYN scan, travessia do gateway ou enumeração de portas. Somente a porta Sentinel configurada é consultada.

Quando `CONNECTED`, discovery para. O heartbeat do Socket.IO mantém a conexão. Após queda, o host conhecido recebe as primeiras tentativas com backoff; discovery volta apenas quando necessário. OFF cancela imediatamente toda atividade da integração.
