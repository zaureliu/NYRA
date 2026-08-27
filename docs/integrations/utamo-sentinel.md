# Utamo Sentinel Bridge

A integração mantém os dois sistemas independentes. O Sentinel detecta e produz alertas; a NYRA descobre uma instância autorizada, mantém um stream read-only e transforma eventos relevantes em histórico, estado visual e fala.

## Ativação

No Sentinel:

1. execute `python scripts/generate_nyra_bridge_token.py`;
2. coloque o token no `.env` privado como `NYRA_BRIDGE_TOKEN`;
3. defina `NYRA_BRIDGE_ENABLED=1`;
4. mantenha `UTAMO_HOST=127.0.0.1` para mesma máquina; para LAN, exponha HTTPS com certificado confiável e use um IP privado literal autorizado;
5. reinicie o Sentinel.

Na NYRA, abra `Settings > Integrations > Utamo Sentinel`, cole o token, salve, ative `Sentinel Watch` e use `Procurar agora`. O token é enviado somente ao backend loopback, gravado em `data/secrets/sentinel-bridge-token.txt` e nunca retornado à interface.

## Controles

- `Sentinel Watch`: controle mestre. OFF fecha o Socket.IO, cancela retry e discovery.
- `Auto Discovery`: testa host manual, último IP conhecido e `127.0.0.1`; o host manual deve ser um IP local literal. LAN exige allowlist privada explícita e HTTPS.
- `Voice Alerts`: controla apenas fala espontânea, não recepção ou histórico.
- `Critical Only`: fala apenas critical e recovery relacionado.
- `Store Event History`: persiste eventos já sanitizados no SQLite local.
- `Create Episodic Memory`: opt-in conservador, somente critical não-replay.
- `Auto Reconnect`: backoff `1,2,5,10,30,60` segundos.

Estados: `DISABLED`, `DISCOVERING`, `FOUND`, `AUTH_REQUIRED`, `CONNECTING`, `CONNECTED`, `RECONNECTING`, `OFFLINE`, `AUTH_FAILED`, `INCOMPATIBLE` e `ERROR`.

## Consultas e ferramentas

As tools `get_sentinel_status`, `get_sentinel_connection_status`, `get_sentinel_recent_events`, `get_sentinel_event_summary` e `search_sentinel_events` são registradas exclusivamente como `READ_ONLY`. Perguntas que mencionam Sentinel recebem um resumo local no contexto; nenhuma ação de escrita é exposta.

Comandos explícitos suportados:

- “Nyra, ativa a busca pelo Sentinel.”
- “Nyra, para de procurar o Sentinel.”

Uma menção casual ao Sentinel nunca muda configuração.

## Transporte autenticado

O token pode trafegar em HTTP apenas para `127.0.0.1`/`::1` literais. O campo manual não aceita hostnames: use um IP local literal, com HTTPS fora de loopback. A descoberta por allowlist gera somente candidatos HTTPS e não faz fallback inseguro; registros antigos de último host em HTTP/LAN são promovidos para HTTPS antes de qualquer probe. Se a instância Sentinel ainda oferecer somente HTTP na LAN, use um proxy TLS local ou mantenha os dois processos no mesmo host.

## Independência

Sem Sentinel, a NYRA mantém LLM, memória, voz, Network Watch e Desktop Presence. Sem NYRA, o Sentinel mantém scanners, banco, UI e alertas. Falha da bridge é fail-open para os dois processos.
