# Network Watch

## Observability V2

O snapshot V2 mantém o contrato plano anterior e adiciona blocos estruturados
`interface`, `quality`, `local_interface`, `gateway_state`, `dns_state` e
`internet_state`. Bytes, pacotes, erros e descartes vêm dos contadores da
interface da rota padrão. RX/TX e pacotes/s são deltas por tempo monotônico;
troca de interface, reset do contador e amostra duplicada reiniciam o baseline
e retornam `null` até a próxima diferença válida. A interface nunca é somada a
adaptadores virtuais não selecionados.

O histórico em memória continua limitado a 900 amostras. A API aceita
`since` em `/api/network-watch/metrics`, permitindo atualização incremental
da UI. O dashboard separa velocidade do link (capacidade nominal) de
throughput (tráfego observado) e exibe `UNAVAILABLE` quando o coletor não
fornece um valor real. Nenhum speedtest é executado.

Eventos são emitidos no EventBus e persistidos com deduplicação por transição
ou cooldown. Testes manuais de latência/recovery permanecem locais e são
marcados como `simulated`; eles não alteram as séries de métricas reais.

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
