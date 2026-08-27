# NYRA Operations UI V3 — Feature Control, Sentinel, Voice Bridge e Release

Implementação do `prompt11_operations_ui_v3_voice_sentinel_release_candidate.md`.
Esta é a **última grande fase funcional** antes de: freeze → documentação PDF → installer.

---

## 1. Shell V3 (`frontend/src`)

```text
┌─────────────────────────────────────────────┐
│ TopStatusBar  NYRA · Ollama · Voz · Backend │
│               Watchdog · Tarefa             │
├──────────────┬──────────────────────────────┤
│ Sidebar      │ Página (ops-content)         │
│ colapsável   │                              │
└──────────────┴──────────────────────────────┘
```

* Design system próprio em `src/ops.css`: navy/graphite, acento ciano,
  escala tipográfica legível (mínimo 12px em metadata; body 14px),
  focus-visible obrigatório e status SEMPRE ícone + rótulo + cor.
* Navegação (`src/ops/Sidebar.tsx`): `overview, conversation, capabilities,
  autonomy, tasks, homelab, network, integrations, sentinel, voice, settings,
  developer, about`. Persistida por hash + localStorage.
* Polling com backoff (`src/ops/hooks.ts`) — cada falha consecutiva dobra o
  intervalo (máx 4×); nada de loop agressivo contra backend offline.
* Cliente HTTP (`src/runtime/api.ts`) normaliza qualquer erro para
  `{code, message, stage, recoverable}`.

## 2. Feature Control Center

Página **Capabilities**: cards por capability com toggle REAL.

Fluxo garantido (`backend/app/core/capabilities.py`):

```text
UI toggle → PUT /api/capabilities/{id} → save_runtime_settings (persistência)
          → setattr(settings) → hot hook (quando existir)
          → probe de verificação → resposta com runtime_state/verification
```

* Capability não-hot mostra `Restart required` após troca (marcação em memória;
  some naturalmente no boot seguinte porque os valores são recarregados).
* Capability derivada (`task_planner`, `recovery_engine`, `openwrt`,
  `desktop_presence`) é honestamente marcada como não-toggleable.
* Falha no hook ⇒ rollback + envelope `CAPABILITY_APPLY_FAILED`.

## 3. Settings Service V3

`backend/app/core/settings_registry.py`: schema declarativo (~60 entradas)
com `key/category/type/current/default/sensitive/requires_restart/description/
validation`.

* `GET /api/settings/v3` — schema+valores; segredos aparecem só como
  `{"configured": true|false}` + `configure_via`.
* `PUT /api/settings/v3` — validação (enum/min/max/tipo), persistência em
  `data/settings-v33.json`; rejeita secret com 409 `SETTING_IS_SECRET`.
* `GET /api/config/export` — export NÃO-secreto para suporte/documentação.

## 4. Integration Center & Sentinel

* `GET /api/integrations/status` — cartões agregados (Sentinel, HA, Proxmox,
  OpenWrt): enabled/configured/connected/state/health/latency/last_sync/last_error.
* `POST /api/integrations/{id}/{test|enable|disable|reconnect|diagnostics}` —
  delega aos serviços existentes; integração offline nunca derruba o backend.
* §91 preservado: OpenWrt com ping OK + SSH falhando = `DEGRADED`
  ("PING_OK_SSH_FALHOU"), nunca OFFLINE.
* Painel dedicado `/sentinel`: conexão, eventos 24h, alertas, hosts do registry
  e configuração completa (componente SentinelSettings). Remote nodes: bridge v1
  não publica — a página diz isso explicitamente (nada fabricado).

## 5. Home Assistant Profiles

`backend/app/integrations/home_assistant_profiles.py`

* Perfis persistidos em `data/ha-profiles.json` (gitignored).
  Seeds: `ha-vm` (ativo default) e `ha-physical` (**desabilitado**, sem URL —
  nunca é contatado: teste retorna 409 `HA_PROFILE_DISABLED`).
* Tokens por perfil em `data/secrets/home-assistant-token-<id>.txt`
  (env `NYRA_HOME_ASSISTANT_TOKEN` tem precedência). Nunca expostos.
* Ativação altera RUNTIME (`HomeAssistantClient.set_credentials`) sem reescrever
  adapter — pronto para hardware físico futuro.
* Teste devolve `api/core_version/state/entity_count/latency_ms`.

## 6. VoiceProcessorBridge (voz externa/local)

`backend/app/speech/external_bridge.py`

* Endpoint default `http://127.0.0.1:8977`; **apenas loopback** (endpoints LAN
  rejeitados). Protocolos: HTTP/WebSocket localhost.
* Capability negotiation via `GET /health` → `{stt,tts,vad,aec,ns,streaming}`.
* Circuit breaker: 5 falhas ⇒ 60s de backoff (`BRIDGE_BREAKER_OPEN`).
* Processor caído ⇒ `fallback_internal_active: true` e o pipeline interno segue;
  chat textual nunca depende daqui.
* Test server E2E: `scripts/test_external_voice_processor.py` (`--fail-after N`
  simula queda).

## 7. Voz V3 na UI

Página Voz: perfis (Realtime/Natural/Low Latency/External Processor — ativação
persistida e aplicada no runtime), entrada (mic + teste), voz (catálogo do
backend `/api/voice/catalog`, volume, test voice), conversação (always listening,
barge-in via settings), processor externo e diagnóstico com métricas reais
(STT latency, LLM TTFT, TTS TTFA, fala→primeiro áudio).

## 8. Release / Sobre / World State

* `GET /api/about` — versão unificada **0.2.0** (Tauri já era 0.2.0; backend,
  frontend e metadados foram unificados nela — ver §AP/AQ do prompt11).
* `GET /api/release/health` — GREEN/YELLOW/RED por critérios: daily-use,
  release gate (`scripts/release_gate.py` → `.tmp/release-health.json`),
  encoding audit. Pendência honesta = YELLOW, não GREEN.
* `GET /api/support/bundle` — versões + capabilities + integrações + erros
  seguros recentes + watchdog. Sem segredos/áudio/memórias/topologia extra.
* `GET /api/world-state` — observações categorizadas (state/source/observed_at/
  freshness/verification).

## 9. Backend hardening nesta fase

* EventBus ganhou `seq` monotônico (WS §158); UI ignora stale por turn_id.
* Novas rotas usam envelope estável `{error_code,message,stage,recoverable}`.
* RuntimeServicesPanel **não auto-aprova mais**: approval de uso único aparece
  para o operador (Aprovar/Negar explícitos).

## 10. Testes novos

Backend: `tests/test_operations_ui_v3.py` (29) +
`tests/test_operations_ui_v3_routes.py` (7 smoke HTTP).
Frontend: `src/ops/opsAudit.test.ts` (ghost buttons, mojibake, navegação) +
`src/ops/ui.test.ts` (status mapping + envelope). Smoke de UI real:
`npm run test:ui` (requer dev server + backend no ar).
