# Network Watch

Network Watch é um monitor assíncrono, read-only e opt-in. Ele nunca altera adaptadores, rotas, DNS ou firewall.

## Probes

- rota padrão do Windows: `route print -4`, chamada fixa e somente leitura;
- gateway: ICMP leve;
- Internet: conexão TCP a pelo menos dois alvos configuráveis (`1.1.1.1:443` e `8.8.8.8:53` por padrão);
- DNS: resolução local de `cloudflare.com`;
- HTTP: recurso pequeno do Microsoft Connect Test;
- interface e throughput: contadores do `psutil`.

Intervalos padrão: interface 1 s, gateway 2 s, Internet 5 s, DNS 15 s e HTTP 30 s. Não há speedtest contínuo.

Latência, jitter (média da variação absoluta entre amostras sucessivas), perda em janela, throughput e estado da rota permanecem em uma janela circular de no máximo 15 minutos. Apenas eventos são persistidos na tabela `network_events` do SQLite.

## Regras

| Evento | Condição inicial |
|---|---|
| Gateway down | 5 s sustentados |
| Internet down | 8 s sustentados |
| DNS failure | 3 falhas consecutivas |
| High latency | média acima de 100 ms por 30 s |
| Very high latency | acima de 200 ms por 15 s |
| Packet loss | acima de 5% por 30 s |
| High jitter | acima de 40 ms por 30 s |

As regras usam histerese e cooldown de cinco minutos. Recuperação produz um único evento. Diagnósticos `LOCAL_LINK_PROBLEM`, `LAN_GATEWAY_PROBLEM`, `DNS_PROBLEM` e `UPSTREAM_PROBLEM` são heurísticos e só aparecem quando as métricas sustentam a inferência.

Ferramentas expostas ao LLM/API: `get_network_status`, `get_network_metrics`, `get_recent_network_events` e `get_network_quality_summary`, todas `READ_ONLY` e com schemas validados.
