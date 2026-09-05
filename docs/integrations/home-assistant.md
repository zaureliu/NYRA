# Integração Home Assistant (REST API)

## Estado atual validado

Instância real do operador acessível por HTTPS (a URL é usada **exatamente como configurada**; a KAZUMI nunca adiciona `:8123` por conta própria). HTTP com Bearer só é aceito em loopback.

Testes manuais prévios confirmaram:

```text
GET /api/        → API running.
GET /api/config  → location_name: Casa · version 2026.8.x · tz America/Sao_Paulo · state RUNNING
GET /api/states  → lista de entidades
```

## Token

Existe um **Long-Lived Access Token dedicado à KAZUMI**. Ele vive somente em:

```env
KAZUMI_HOME_ASSISTANT_ENABLED=true
KAZUMI_HOME_ASSISTANT_URL=https://home-assistant.local
KAZUMI_HOME_ASSISTANT_TOKEN=<SECRET>
```

O token NUNCA aparece em código, registry, logs, histórico, contexto do LLM ou respostas da API (`public_dict` mascara como `***configured***`). Vai exclusivamente no header `Authorization: Bearer …`.

Criar/renovar o token no HA: Profile → Security → *Long-Lived Access Tokens* → nome sugerido `kazumi`.

## Endpoints suportados

```text
GET  /api/
GET  /api/config
GET  /api/states
GET  /api/states/<entity_id>
POST /api/services/<domain>/<service>
```

## Tools

| Tool | Risco | Descrição |
|---|---|---|
| `ha_status` | READ_ONLY | API running + Core state + versão + location_name + entity count |
| `ha_list_entities` | READ_ONLY | filtros `domain`, `state`, `search`, `limit` |
| `ha_get_state` | READ_ONLY | estado e atributos limitados de uma entidade |
| `ha_call_service` | LOW_RISK/ELEVATED | service call estruturado com target/service_data |

Mutações ficam desabilitadas por padrão. Após `KAZUMI_HOMELAB_MUTATIONS_ENABLED=true`, toda chamada de serviço exige approval de uso único; pares fora da allowlist também sobem para risco ELEVATED.

URL arbitrária não existe na superfície: a tool só aceita `domain`, `service`, `target` (entity_id/device_id/area_id) e `service_data`.

## Grounding (ACT → VERIFY)

```text
light.turn_off
↓ service call aceito (HTTP 2xx)  ≠ efeito confirmado
↓ GET /api/states/<entity>
↓ state == off?
→ effect_verified = true/false
```

O resultado da tool traz `effect_verified` e `verification_status`; quando o efeito não é verificável automaticamente, a resposta diz isso explicitamente em vez de afirmar sucesso.

## Sem dispositivos IoT

A integração funciona mesmo sem lâmpadas/sensores — status, entidades e contagem são observação válida. Para validar o ciclo de ação com segurança, crie manualmente no HA um helper `input_boolean.kazumi_test` e use:

```text
ha_call_service input_boolean.turn_on  → state=on  (verified)
ha_call_service input_boolean.turn_off → state=off (verified)
```

## Testes

```powershell
python scripts/homelab-smoke.py --only ha     # usa o .env real; nunca imprime o token
```

Erros normalizados: `HA_AUTH_MISSING` · `HA_AUTH_FAILED` · `HA_API_UNAVAILABLE` · `HA_ENTITY_NOT_FOUND` · `HA_SERVICE_FAILED`.

WebSocket API e eventos realtime ficam preparados para uma fase futura (V2).


## V11 — Resolução única de credencial + guardas de regressão (prompt11_1)

A regressão de `invalid authentication` em `GET /api/` (UAs `KAZUMI-Homelab/1.0` e
`python-httpx/0.28.1`) foi corrigida na fonte. Regras vigentes:

- **Resolução autoritativa única** (`resolve_profile_token`): perfil ativo →
  env `KAZUMI_HOME_ASSISTANT_TOKEN` → Credential Broker
  (`homeassistant_token_<profile>`) → arquivo legado `data/secrets/…`
  (migrado em silêncio para o Broker) → settings legadas (.env via pydantic).
- **Guarda no client** (`home_assistant.py`): endpoint autenticado
  (`/api/`, `/api/config`, `/api/states`, `/api/services/*`) sem token levanta
  `HA_AUTH_MISSING` ANTES de qualquer pacote sair — nenhum 401 é gerado.
- **Guarda no monitor** (`homelab/controller.py`): com token ausente o ciclo
  nem consulta a API (`INTEGRATION_UNAVAILABLE / HA_AUTH_MISSING`).
- **Test Connection** (`_probe`): UA identificado da KAZUMI, exige Bearer,
  estados `READY | AUTH_FAILED | UNCONFIGURED | OFFLINE | HA_TIMEOUT |
  HA_TLS_ERROR`; nunca `READY` sem auth validada (invariante testada).
- Estados do card único para Homelab + Integrations via
  `unified_ha_state()` — impossível divergirem.

Novos endpoints: `GET /api/home-assistant/entities`, `GET
/api/home-assistant/entities/{id}`, `POST …/{id}/service` (com readback
VERIFY). Realtime continua NOT AVAILABLE (fase futura).


## V11.2 — Refresh do STALE pelo monitor (prompt11_2)

Sintoma corrigido: card exibia `STALE · Último sucesso há 954s` mesmo com a
autenticação saudável. Causa: o health loop do Homelab sondava a API HA com
sucesso a cada ciclo, mas `last_success` da fonte única da UI só era
atualizado por teste manual — após 900s o card ficava STALE indefinidamente.

- Sucesso AUTENTICADO do monitor agora registra `last_test`/`last_success`
  no perfil ativo (`record_monitor_success`, cooldown 30s, gravação atômica).
- Falhas de rede do monitor não alteram o estado da UI — falha continua vindo
  de teste real (nada de OFFLINE/AUTH_FAILED inventado por probe de rede).
- HA saudável volta a `READY` no próximo poll; STALE só aparece se os probes
  realmente pararem (honesto, não cache antigo).
