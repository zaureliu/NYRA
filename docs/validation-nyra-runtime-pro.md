# NYRA — Validação de Runtime (Edição PRO OpenCode)

Data: 2026-08-26 · Sessão: Pente-fino máximo unificado (OpenCode)
Branch: `feature/nyra-avatar-v2` @ `6114452` (working tree pré-existente preservado)

## Runtimes utilizados

| Runtime | Uso nesta validação |
|---|---|
| Dev (`uvicorn` :8010 via `.venv`) | TODOS os E2Es deste documento |
| Frozen (`nyra-backend.exe` :8000) | Fechado com consentimento do operador durante o E2E (conflito de VRAM); reaberto ao final |
| Ollama :11434 | Compartilhado; modelo residente gerido por warm manager |

## VEREDITO GERAL: PASS com bloqueio externo documentado

## CHAT
- Fast path de conversa trivial implementado (`realtime/orchestrator.py`): pulsa percepção/sentinel/network; `tools_ms ≈ 0`.
- E2E real: "Oi, tudo bem?" → TTFT 536–949 ms, total ~13–23 s, `prompt_chars≈2600`, zero tools.
- Log PERF agora registra `model=<ativo>`.
- **Bloqueio externo**: `qwen3:8b` não carrega neste Ollama/GPU (llama-server não sobe — GPU e CPU; ver PROBLEMAS). E2E executado com `wrench-9b:q4_k_m`; default oficial restaurado para qwen3:8b ao final.

## ROUTING / TOOL SUBSETS
- `classify_domain()` + `llm_tools(domain)`: CONVERSATION=0 schemas; DESKTOP/HOMELAB/NETWORK/FILESYSTEM/RUNTIME/BROWSER recebem subset + suporte mínimo; GENERIC = fallback completo.
- Cache de schemas Pydantic (`lru_cache`).
- Router: imperativo sem alvo conhecido roteia ao agente ("rode os testes").
- Testes: `backend/tests/test_pro_routing.py` (20 PASS).

## DESKTOP OPERATOR
- Alias VS Code (`_KNOWN_APPS` + `desktop_apps.yaml`, caminho `%LOCALAPPDATA%` expandido em runtime).
- Recovery de launch: OSError no Popen → ShellExecuteW com verificação pid-free.
- `open_file/open_url` honestos: `effect_verified=null` sem probe de janela.
- Semântica read_only corrigida: LOW_RISK passa quando política permite; ELEVATED+ bloqueado.
- **E2E real**: "Nyra, abre o bloco de notas." via `/api/chat` → notepad REAL aberto, PID confirmado por probe externo, fechado depois. Resposta grounded: "Confirmado: Bloco de Notas está aberto e visível (PID …)".

## TTS / CORRELAÇÃO POR TURNO
- `_SpeechItem` agora tem `conversation_id`+`created_at`; auto-tagging de rotas sem turno.
- `purge_except()` a cada novo turno (stale audio = 0 garantido; CRITICAL preservado).
- Contadores Apêndice C expostos em `/api/realtime/debug`: created/played/cancelled/stale_dropped/order_violations.
- E2E: dois turnos com áudio → fila zerada, violations=0.

## AUTONOMIA / EVENTOS
- EventBus: assinante lento (timeout 0,5 s blindado) segue em background; publisher nunca bloqueia; contadores + `detached_handler_tasks()`.
- World State: freshness honesta (Homelab usa `generated_at` real; Network usa timestamp do snapshot).
- Proactive default OFF conferido.

## HOMELAB (estado REAL durante E2E)
- Proxmox ONLINE · OpenWrt ONLINE · Home Assistant ONLINE (perfil ha-vm) · dc1 UNREACHABLE (host desabilitado).
- Inventário real: 6 VMs, 3 running (Ubuntu-Utamo-Server, utamo-mail, home-assistant).

## VOICE SATELLITE / nyra.voice.v1
- NYRA WS: `hello→hello_ack`, `voice.barge_in/tts.stop → cancel_speech()` (só TTS), heartbeat ack, lock de saída.
- Satellite: HELLO versionado + handshake obrigatório; estado CONNECTING usado; backoff reset corrigido; dedup HTTP+WS; health "nyra"/"vad" reais; `.env.example` corrigido (variáveis eram inertes).
- E2E WS: `{"connected":"CONNECTED","hello_ack":true,"heartbeat_ack":true}`.
- Bridge :8977 ciclo real: OFFLINE+fallback ON → HEALTHY (processor conectado) → OFFLINE+fallback ON.

## CORREÇÃO DE ORIGEM CRÍTICA (descoberta no E2E)
Templates rígidos de chat (wrench/qwen3 no Ollama) rejeitam `system` fora do início e pares de `assistant` no fim — derrubavam QUALQUER turno/agente com HTTP 500/400. `OllamaProvider._wire_messages()` normaliza o wire: sistemas líderes viram cabeçalho; diretivas tardias são recodificadas como mensagem do usuário `[INSTRUÇÃO DO SISTEMA]`. Vale para chat, agent loop e correções de grounding.

## LATÊNCIAS (amostra única, warm)
| Métrica | Valor |
|---|---|
| Chat TTFT (Ollama) | 536–949 ms |
| Chat total (turno simples) | ~13–23 s |
| tools_ms fast path | ~0–2 ms |
| context_build_ms | ~7 ms |

## TESTES
- Backend: **585 passed** (inclui novos: test_pro_routing 20, test_pro_tts_queue 4, eventos slow-subscriber).
- Satellite: **62 passed** (58 existentes + test_voice_v1_contract).
- `git diff --check`: limpo.

## PROBLEMAS RESTANTES
1. **qwen3:8b BLOQUEADO no ambiente** — llama-server não inicia (GPU nem CPU), `Load failed: timed out waiting for llama-server to start`. Outros modelos carregam normalmente. Provável GGUF/versão do Ollama; sugestão ao operador: re-puxar o modelo ou atualizar Ollama. Fallback aponta para ele — enquanto não funcionar, qualquer falha transitória de LLM degrada para esse modelo que não sobe (fallback_enabled pode ser desligado em brain-settings.json).
2. Testes 9 e 10 (voz real via microfone e barge-in audível) exigem presença do operador — pipeline validado até STT/TTS/playback por contadores e eventos; áudio audível não pôde ser afirmado sem humano.
3. Multi-step desktop (digitar/salvar arquivo) permanece para validação manual.
4. Correlação multifonte continua como disciplina de prompt (sem invenção de causalidade).

## ARQUIVOS ALTERADOS (resumo)
Backend: tools/registry.py, tools/agent.py, realtime/orchestrator.py, character/context.py, llm/ollama.py, speech/queue.py, events/bus.py, core/release_info.py, api/routes.py, desktop/{discovery,models,control}.py · config/desktop_apps.yaml · testes novos/atualizados.
Satellite: app/nyra/{heartbeat,reconnect,client,protocol}.py, app/pipeline.py, .env.example, tests/test_voice_v1_contract.py.

---

# FULL COMPUTER OPERATOR UNIVERSAL V1 (nyra-full.md) — 2026-08-26

## APP DISCOVERY (fontes reais desta máquina)
```
Start Menu:            185
App Paths:             46  (HKCU 1 + HKLM 45)
Uninstall registry:    41
PATH:                  127
UWP / Get-StartApps:   74
Common dirs:           3
shell_known:           11
Total unique applications indexed: 487
Total aliases:                    ~1089 (+ aprendidos)
Persistência: data/app-registry/{index.json, learned.json}
Refresh: startup + a cada 6h + POST /api/apps/registry/refresh
```

## PIPELINE
- Universal Intent Router: `desktop/intents.py` — PT-BR sem LLM; texto e voz entram pelo mesmo `/api/chat`.
- Target Resolver: alias aprendido → índice exato → fuzzy (`ApplicationDiscovery`), com dedup de candidatos que apontam para o MESMO executável.
- Universal Action Router: fast path no orchestrator (OPEN/CLOSE/MINIMIZE/MAXIMIZE/RESTORE/FOCUS, pastas shell:, multi-step notepad). ONE ACTION OWNER por turno.
- Effect Verifier: PID+HWND+janela visível obrigatórios; app já aberto → ALREADY_OPEN com foco (honesto); console apps via ShellExecuteW (PE subsystem detectado).
- Contexto: `last_controlled` atende "fecha ele/minimiza isso".
- APIs Developer: `/api/apps/registry/status|refresh|diagnostics`.

## E2E REAL (via /api/chat, mesmo caminho da UI)
| Input | Target | Method | PID | Verified | Delta | Result |
|---|---|---|---|---|---|---|
| abre o bloco de notas | Notepad | EXE detached | ✔ | ✔ | +1 | PASS |
| abre a calculadora | Calculator (UWP) | shell:AppsFolder | ✔ | ✔ | +1 | PASS |
| abre o code | VS Code | EXE (%LOCALAPPDATA%) | ✔ | ✔ | +família | PASS |
| abre o edge | Microsoft Edge | lnk/App Paths dedup | ✔ | ✔ | ✔ | PASS |
| abre o paint | Paint | EXE | ✔ | ✔ | +1 | PASS |
| abre o gerenciador de tarefas | Taskmgr | single-instance → foco | ✔ | ✔ | 0 (já aberto) | PASS |
| abre o powershell | PowerShell | console→ShellExecuteW | ✔ | ✔ | +1 | PASS |
| abre as configurações do windows | Settings (ms-settings) | URI | ✔ | ✔ | +1 | PASS |
| abre a pasta downloads | shell:Downloads | URI | explorer | ✔ | n/a* | PASS |
| abre o terminal | wt.exe NÃO instalado | — | — | honesto | 0 | NOT_FOUND correto (§39) |
| fecha X / minimiza... | janelas reais | WM_CLOSE/Win32 | ✔ | ✔ | — | PASS |

*explorer sempre ativo; verificação pela resposta grounded.

## VOICE
Mesmo pipeline estrutural (STT → /api/chat → intents). Teste audível: MANUAL_REQUIRED.

## MULTI-STEP
`abre o bloco de notas, escreve "NYRA teste" e salva na área de trabalho como nyra-teste.txt`
→ executor determinístico `desktop/multistep.py`: launch✔ focus✔ type(UIA read-back)✔ Ctrl+S✔ diálogo✔ path+Enter✔ **arquivo criado e conteúdo verificado** (13.8s).

## GROUNDING
false success claims: **0** (falha real → resposta honesta; open_file/url sem probe = não-verificado).

## DUPLICATION
same-turn double-launch: **0** (E2E com turn_id repetido → delta exatamente 1).

## LIMITAÇÕES
- Windows Terminal não instalado na máquina (NOT_FOUND honesto).
- Painel UI mínimo (§29) não adicionado — endpoints prontos p/ seção futura.
- Voz audível depende do operador (microfone).
