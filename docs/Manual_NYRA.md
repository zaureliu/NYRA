# NYRA 0.2.0 — Manual Pessoal e Referência Rápida

> Documento de uso pessoal. Gerado a partir do estado real do código em
> `<NYRA_ROOT>` (branch `feature/nyra-avatar-v2`, commit `6114452`), em 2026-08-24.

---

## Sumário

1. [Visão geral](#1-visão-geral)
2. [Estrutura de alto nível](#2-estrutura-de-alto-nível)
3. [Como iniciar](#3-como-iniciar)
4. [Como encerrar / reiniciar](#4-como-encerrar--reiniciar)
5. [Interface](#5-interface)
6. [Conversa e Agent](#6-conversa-e-agent)
7. [Voz](#7-voz)
8. [Integrações](#8-integrações)
9. [Homelab](#9-homelab)
10. [Capabilities](#10-capabilities)
11. [Local Operator](#11-local-operator)
12. [Segurança](#12-segurança)
13. [Arquivos e diretórios importantes](#13-arquivos-e-diretórios-importantes)
14. [Configuração](#14-configuração)
15. [Backup pessoal](#15-backup-pessoal)
16. [Troubleshooting](#16-troubleshooting)
17. [Limitações atuais](#17-limitações-atuais)
18. [Versão](#18-versão)

---

## 1. Visão geral

A NYRA é uma assistente pessoal local-first que roda no Windows: um backend
FastAPI na própria máquina (`127.0.0.1:8000`), uma interface web (React/Vite)
apresentada dentro de um app desktop Tauri, e o Ollama como servidor de LLM
externo (`127.0.0.1:11434`).

Finalidade: conversa por texto/voz, automação do próprio ambiente (shell,
desktop, browser) com aprovação explícita do operador, monitoramento de rede e
homelab (OpenWrt, Proxmox, Home Assistant) e integração opcional com o bridge
UTAMO Sentinel.

Princípios do projeto:

- **Local-first**: nada sai da máquina sem consentimento; serviços externos são opt-in.
- **Segredos nunca em plaintext**: Credential Broker com Windows Credential Manager.
- **Nada executa sem schema/risco/aprovação**: shell, SSH e ações sensíveis passam por classificação de risco e Approval Gate.
- **Estados honestos**: a UI mostra UNCONFIGURED/AUTH_FAILED/OFFLINE em vez de inventar status.

Arquitetura local resumida:

```text
[Usuário] → NYRA Desktop (Tauri, bandeja + painel)
                └── Frontend React (Operations UI) — embutido no release
                        └── HTTP/WS → Backend FastAPI (127.0.0.1:8000)
                                          ├── LLM ←→ Ollama (11434, externo)
                                          ├── Voz (STT/TTS locais ou edge-tts)
                                          ├── Tools (system_shell, remote_shell, agent…)
                                          ├── Local Operator / Runtime Supervisor
                                          └── Integrações/Homelab (HA, Proxmox, OpenWrt, Sentinel)
```

---

## 2. Estrutura de alto nível

| Bloco | Onde vive | O que faz |
|---|---|---|
| Desktop/Tauri | `desktop/src-tauri` | Presença desktop (janela always-on-top click-through), bandeja, atalhos globais, abre o painel; no modo instalado também inicia/vigia o `nyra-backend.exe` |
| Frontend | `frontend` (React + Vite) | Operations UI V3 (páginas da seção 5); no release é embutida no executável (nada de porta 5173) |
| Backend FastAPI | `backend/app` | API REST/WS em 127.0.0.1:8000; orquestra tudo abaixo |
| Conversation Engine | `backend/app/conversation` | Turnos de conversa, STT→LLM→TTS, isolamento por turno |
| LLM/Ollama | `backend/app/llm`, `app/brain` | Provider Ollama (qwen3:8b por padrão), warm manager, benchmark lab |
| Voice | `backend/app/speech` | Faster-Whisper (STT), TTS kokoro/chatterbox/edge_tts/pyttsx3 com fallback, Always Listening, fila de fala |
| Tools/Agent | `backend/app/tools`, `app/agent` | Registry de tools com schemas Pydantic e risco; system_shell e remote_shell (SSH via Trusted Host Registry); AgentController |
| Local Operator | `backend/app/operator`, `app/desktop` | Automação local: desktop/UIA, browser, credenciais, jobs/tasks/workflows, elevated sessions |
| Runtime/Watchdog | `backend/app/runtime`, `watchdog/` | Supervisor de serviços gerenciados, histórico; watchdog externo só em dev |
| Homelab | `backend/app/homelab` | Control Plane: probes ICMP/SSH/TCP, adapters OpenWrt/Linux/Windows, histórico e eventos |
| Integrations | `backend/app/integrations` | Home Assistant (profiles), Proxmox (read-only), OpenWrt, Sentinel, Integration Center |

---

## 3. Como iniciar

Três modos reais. Todos usam backend em **127.0.0.1:8000**.

### 3.1 Modo desenvolvimento (web)

```powershell
cd <NYRA_ROOT>
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start.ps1
```

Sobe uvicorn (venv `.venv`) + Vite dev server. UI em `http://127.0.0.1:5173`.
Logs em `logs\`. Encerra com `scripts\stop.ps1`.

### 3.2 Modo executável/release

```powershell
cd <NYRA_ROOT>
powershell -NoProfile -ExecutionPolicy Bypass -File build-nyra.ps1      # compila frontend+tauri
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-nyra.ps1   # sobe ollama/backend/exe
```

O launcher garante Ollama, inicia o backend pelo `.venv` e abre
`desktop\src-tauri\target\release\nyra-desktop.exe`. Estado em `.nyra-runtime.json`.

### 3.3 Modo instalado

Instalador NSIS (`NYRA-Setup-x64.exe`) instala em `C:\Program Files\NYRA Desktop`
e os dados vão para `%LOCALAPPDATA%\NYRA` (`config`, `data`, `logs`, `cache`,
`backups`, `workflows`). Abrir a NYRA pelo atalho:

- o app verifica `127.0.0.1:8000`;
- se já existe um backend NYRA válido, reutiliza;
- se a porta estiver ocupada por outro processo, reporta `BACKEND_PORT_CONFLICT` (não mata nada);
- se estiver livre, inicia o `nyra-backend.exe` embutido e espera `/health` ficar saudável.

Requisitos externos: **Ollama** rodando em 11434 (se offline, a UI mostra offline — o resto continua funcionando).

### 3.4 Requisitos

- Windows 10/11 x64
- Python 3.11 + `.venv` (dev/release) — desnecessário no modo instalado
- Node.js + npm (apenas para desenvolver/compilar)
- Rust/toolchain Tauri (apenas para compilar o desktop)
- Ollama + modelo (padrão `qwen3:8b`) para o cérebro LLM

---

## 4. Como encerrar / reiniciar

Confirmado no código atual (`lifecycle.py`, `backend_manager.rs`):

- **Fechar janelas normalmente**: ao fechar a última janela, o app sai e encerra o backend *owned* que ele mesmo iniciou; a porta 8000 é liberada.
- **Encerrar NYRA completamente**: bandeja → "Encerrar NYRA". Marca shutdown intencional, encerra o backend owned e não relança.
- **Reiniciar NYRA completamente**: ação `restart` da runtime API (`POST /api/runtime/action` com `{"action": "restart"}`). No modo instalado o backend sai com código 75 e o Tauri abre uma nova sessão sozinho; em dev usa o launcher PowerShell legado.
- **Backend não-owned**: se o backend já estava rodando antes do app, ele é reutilizado e **não** é morto no fechamento.
- **Porta 8000 ocupada por processo externo**: evento `BACKEND_PORT_CONFLICT`; a NYRA não mata o processo alheio.

Watchdog: existe apenas em desenvolvimento (`watchdog/nyra_watchdog.py`).
No pacote instalado ele fica desabilitado (não há Python/PowerShell no instalador).

---

## 5. Interface

Painel Operations UI V3 — menu lateral com as páginas reais:

| Página | Propósito |
|---|---|
| Visão geral | Feed de eventos, saúde dos subsistemas, release health |
| Conversa | Chat com a NYRA, transcrições, traces de tools |
| Capabilities | Liga/desliga capacidades; mostra enabled/configured/restart_required |
| Autonomia | Proactive engine, limites e gates de autonomia |
| Tarefas | Jobs, tasks e workflows do operador (retomada pós-restart) |
| Homelab | Painel do control plane: hosts, probes, métricas, serviços |
| Rede | Network Watch: dispositivos, alertas, quiet mode |
| Integrações | Cartões Sentinel/Home Assistant/Proxmox/OpenWrt com Testar/Configurar/Diagnóstico |
| Sentinel | Bridge UTAMO: conexão, eventos, reconexão |
| Voz | Voz lab: perfis, pronúncia, voice hunter, bridge externo |
| Configurações | Settings V3 por categoria (homelab, integrações, privacidade…) |
| Developer | Flags de debug, encoding audit, runtime services |
| Sobre | Versão, componentes, licença |

Além do painel, existe a **presença desktop**: mini-janela always-on-top
(click-through com Ctrl+Shift+I), controlada pela bandeja.

---

## 6. Conversa e Agent

- **Chat**: turnos com isolamento (`TurnRegistry`) — cada turno tem seu contexto; falhas de pipeline viram erros tipados.
- **Tools**: o agente chama ferramentas registradas com schema Pydantic e nível de risco (`READ_ONLY`, `LOW_RISK`, `ELEVATED`, `DESTRUCTIVE`, `CRITICAL`).
- **Approvals**: ações elevadas/destrutivas exigem approval de uso único (fingerprint vinculado). Texto gerado pelo LLM **nunca** concede aprovação.
- **Grounding**: o agente só afirma execução com resultado real de tool; sem chamada = resposta honesta de erro ("NUNCA afirme que executou…").
- **Shell local**: todo comando passa por `system_shell` com classificação de risco, timeout, redaction e auditoria; executáveis desconhecidos não são presumidos seguros.
- **SSH remoto**: apenas hosts do Trusted Host Registry (`remote_shell`), com usuário/chave cadastrados fora do LLM.
- **Modo técnico/traces**: a página de conversa expõe traces das tools quando habilitado nas configurações de developer.

---

## 7. Voz

Estado real (dependências presentes no ambiente de dev):

- **STT**: Faster-Whisper (modelo `tiny` por padrão, CPU int8, VAD embutido).
- **TTS**: cadeia com fallback — kokoro-onnx → chatterbox (venv separado `.venv-chatterbox`) → edge_tts → pyttsx3. Sem modelo local, edge/pyttsx3 seguem funcionando.
- **Always Listening**: wake word "Nyra" / hands-free com timeouts e indicador de privacidade; áudio de debug é opt-in.
- **Voice profiles**: perfil de referência e laboratório de pronúncia (dicionário + overrides persistidos).
- **External Voice Processor Bridge**: ponte HTTP/WS opcional (porta 8977) para processar voz fora do processo principal; desabilitada por padrão.
- **Voice Hunter**: comparação/benchmark de vozes TTS com gravação e score.

No modo **instalado**, os engines que dependem de venv/modelos locais
(chatterbox, kokoro) não estão empacotados — edge_tts/pyttsx3 assumem.

---

## 8. Integrações

Cartões na página **Integrações**, com estados coerentes:
`DISABLED · UNCONFIGURED · AUTH_MISSING/AUTH_FAILED · OFFLINE · DEGRADED · READY · STALE`.

### Home Assistant

- Perfis múltiplos (`data/ha-profiles.json`) com URL + TLS por perfil.
- Token long-lived vai **só** para o Credential Broker; a UI recebe apenas `auth_configured`.
- Ativar perfil, testar conexão (Bearer obrigatório), entity browser somente-leitura.
- Sem token: estado UNCONFIGURED — nunca READY.

### Proxmox VE

- Configuração completa pela UI: URL, verify TLS, timeout, node preferido.
- API Token ID + Secret salvos em par no Credential Broker; vazio nunca sobrescreve credencial salva.
- Inventário real quando autenticado: nodes, QEMU, LXC, storage.
- Power operations (start/shutdown/reboot/stop) passam pelo executor do homelab **com aprovação single-use** e verificação de efeito.

### OpenWrt

- Card "Configurar": Host/URL, Usuário SSH e Senha (senha → Credential Broker, write-only).
- "Testar conexão" usa o adapter SSH existente (`OpenWrtAdapter` via Trusted Host Registry) — leituras ubus READ_ONLY.
- Estados comuns: `UNCONFIGURED` (sem host/senha), `AUTH_FAILED`/`REMOTE_AUTH_FAILED` (credencial recusada), `OFFLINE` (host inalcançável), `READY` (status ubus OK).
- A senha salva **não** altera o transporte SSH do registry (chave/agente continuam valendo).

### UTAMO Sentinel

- Bridge opcional; desabilitado por padrão nas configs atuais.
- Conector com auto-reconnect, eventos, cooldown de alertas por voz.
- Limitação: depende do bridge externo UTAMO rodando; sem ele, estado OFFLINE/DISABLED.

---

## 9. Homelab

Control plane único (`HomelabControlPlane`) alimentado por um registry local
de hosts confiáveis (`config/homelab_hosts.yaml` — não publicado). Cada host
tem probes ICMP/TCP/SSH, estado agregado e histórico em SQLite.

- **OpenWrt**: adapter SSH (ubus/ifstatus/logread), Wi-Fi, logs, WAN/LAN.
- **Proxmox**: cliente API read-only compartilhado; power ops só com approval.
- **Home Assistant**: monitor autenticado + entidades.
- **DC1/Windows/Linux**: adapters genéricos conforme tipo cadastrado no registry.
- **Sentinel**: aparece como integração/cartão; probes isoladas não derrubam o agregado.

Eventos relevantes viram feed na Visão geral e podem disparar alertas de voz
(com cooldown) quando as capacidades correspondentes estão ligadas.

---

## 10. Capabilities

Página **Capabilities** lista capacidades reais do sistema com três conceitos:

- **enabled**: ligada/desligada (persistido em `data/settings-v33.json`);
- **configured**: possui o que precisa para funcionar (ex.: token configurado);
- **runtime health**: estado vivo agora (`READY`, `DEGRADED`, `FAILED`…).

Toggles marcados `restart_required` precisam de reinício do backend para valer.
Capacidades notáveis: conversation engine, always listening, network watch,
sentinel alerts, proactive mode, watchdog (dev), credential broker, runtime supervisor.

---

## 11. Local Operator

Automação do **próprio** ambiente, sempre com approval nos passos sensíveis:

- **Desktop**: apps cadastrados (`config/desktop_apps.yaml`), UI Automation, foco/janela, power actions críticas com 30s de cancelamento.
- **Browser**: controller próprio (perfil dedicado) para navegação/leitura local.
- **Shell**: system_shell (local) e remote_shell (SSH confiável), com risco/timeout/redaction/histórico.
- **Filesystem**: leitura/escrita restritas a escopos aprovados.
- **Runtime**: supervisor de serviços com restart limitado e janela de cooldown.
- **Jobs/Workflows**: tasks persistentes com retomada segura pós-restart e workflows versionáveis (`config/workflow_templates.json` → `data/workflows.json`).

Foco é operação doméstica/local; nada aqui ensina ou facilita uso contra terceiros.

---

## 12. Segurança

- **Credential Broker**: fachada sobre Windows Credential Manager (targets `NYRA_CRED:*`) com fallback DPAPI (`credentials-vault.bin`). O LLM só vê `credential_id`; secrets saem apenas por injeção interna.
- **Secrets fora de settings**: tokens do HA/Proxmox/Sentinel/OpenWrt nunca aparecem em logs, UI ou exports; settings públicos mascaram.
- **Approval Gate**: riscos ELEVATED/DESTRUCTIVE/CRITICAL exigem approval single-use vinculado por fingerprint.
- **UAC legítimo**: elevação passa pelo fluxo do Windows; não há bypass.
- **Grounding**: respostas de agente exigem evidência de tool.
- **Redaction**: exceções/logs passam por filtro de segredos.
- **Sem execução por texto livre**: nada do que o LLM escreve roda direto no shell.

---

## 13. Arquivos e diretórios importantes

Baseado no diretório real (`<NYRA_ROOT>` = pasta do projeto):

```text
<NYRA_ROOT>
├── backend\
│   ├── app\            # código do FastAPI (módulos por domínio)
│   ├── tests\          # pytest
│   ├── run_backend.py  # entrypoint standalone (empacotado)
│   └── requirements.txt
├── frontend\           # React/Vite (dashboard + desktop presence)
├── desktop\src-tauri\  # shell Tauri/Rust (+ backend_manager.rs)
├── config\             # default.yaml, runtime_services.yaml, templates…
├── data\               # nyra.db, settings-v33.json, workflows.json…
├── docs\               # documentação técnica gerada durante o desenvolvimento
├── identity\           # personalidade/prompts/perfil de voz
├── logs\               # saída de runtime (dev)
├── scripts\            # start/stop/build/validações (helpers de dev)
└── packaging\          # staging/build do nyra-backend.exe
```

Modo instalado (dados do usuário): `%LOCALAPPDATA%\NYRA` com
`config`, `data`, `logs`, `cache`, `backups`, `workflows`, `identity`.

---

## 14. Configuração

Precedência real (backend): variáveis `NYRA_*`/`.env` > overlay persistido > `config/default.yaml` > defaults do código.

- **Não secretas**: página Configurações (Settings V3) persiste overlay em `data/settings-v33.json`; defaults em `config/default.yaml`.
- **Secretas**: exclusivamente no Credential Broker (Credential Manager). Campos secretos na UI mostram apenas `configured: true/false` e nunca voltam em plaintext.
- **Persistidos**: banco SQLite (`data/nyra.db`), perfis HA (`ha-profiles.json`),
  configs de integração (`proxmox-config.json`, `openwrt-config.json`), workflows, benchmarks.
- **Registry de hosts homelab**: `config/homelab_hosts.yaml` (arquivo local; não vai para o repositório público).

No modo instalado todos esses caminhos vivem sob `%LOCALAPPDATA%\NYRA\...`.

---

## 15. Backup pessoal

Com a NYRA **parada**, salvar:

1. `config\` inteiro (inclui registry de hosts e templates);
2. `data\` essencial: `nyra.db`, `settings-v33.json`, `workflows.json`,
   `ha-profiles.json`, `proxmox-config.json`, `openwrt-config.json`,
   `credentials-vault.bin` (fallback DPAPI);
3. `docs\` e este manual;
4. Credenciais do Windows Credential Manager (targets `NYRA_CRED:*`) ficam no
   perfil do usuário — backup do perfil do Windows cobre; **não existe
   exportador dedicado** no projeto (não invente um).

Restaurar = recolocar as pastas antes de iniciar o backend.

---

## 16. Troubleshooting

| Sintoma | Significado | Verificação | Ação segura |
|---|---|---|---|
| Backend offline | API 8000 não responde | `GET http://127.0.0.1:8000/health` | Modo instalado: fechar e reabrir a NYRA. Dev: `scripts\start.ps1`; checar `logs\backend.stderr.log` |
| Porta 8000 ocupada | Outro processo escutando | `Get-NetTCPConnection -LocalPort 8000` | NYRA reporta `BACKEND_PORT_CONFLICT` e NÃO mata o processo; encerre o processo externo manualmente se for legítimo |
| `BACKEND_PORT_CONFLICT` no evento | Ídem, detectado pelo app | Identificar PID acima | Mover o conflito ou parar o processo estranho |
| Ollama offline | LLM indisponível | `GET http://127.0.0.1:11434/api/tags` | Iniciar Ollama; NYRA segue degradada (sem cérebro) |
| Integração UNCONFIGURED | Falta config/credencial | Cartão Integrações → Configurar | Preencher campos; senha/token vão ao broker |
| AUTH_FAILED | Credencial recusada pelo serviço remoto | Cartão → Testar conexão | Refazer token/senha no serviço de origem e salvar de novo |
| TLS_ERROR | Certificado inválido/autoassinado (Proxmox) | Config do cartão | Ajustar TLS verification conscientemente |
| STALE | Último teste ok mas antigo | Cartão → Testar conexão | Rodar novo teste para refrescar |
| Frontend "bundle antigo"/página branca no exe | Release carregando dist velho | Conferir data de `frontend\dist` e do `.exe` | Rodar `build-nyra.ps1` de novo |
| Watchdog não reinicia backend (instalado) | Watchdog não existe no pacote | — | Comportamento documentado; usar Encerrar/Reabrir |

---

## 17. Limitações atuais

Encontradas no estado atual do código/pacote:

- Watchdog não é empacotado no instalador (desabilitado); crash inesperado do backend não auto-relança — só restart intencional relança.
- Voz local avançada (chatterbox/kokoro) depende de venv/modelos que não vão no instalador; fallback edge_tts/pyttsx3.
- Live2D/assets de avatar pesados não fazem parte do repositório público.
- Registry de hosts homelab (`homelab_hosts.yaml`) é arquivo local pessoal — não distribuído.
- Bridge Sentinel depende de serviço UTAMO externo.
- Sem telemetria/nuvem por design: nada de sync/remoto.

---

## 18. Versão

- **Versão**: 0.2.0 (`backend/pyproject.toml`, `desktop/src-tauri/tauri.conf.json`)
- **Origem do manual**: branch `feature/nyra-avatar-v2`, commit `6114452`
- **Data de geração**: 2026-08-24
