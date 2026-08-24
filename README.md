# NYRA

Assistente pessoal **local-first** para Windows: conversa por texto e voz,
automação do próprio ambiente com aprovação explícita, monitoramento de rede e
homelab (OpenWrt / Proxmox / Home Assistant) — tudo rodando na sua máquina, sem
nuvem obrigatória.

> Projeto pessoal em desenvolvimento contínuo. Não é um produto comercial.

## O que ela faz

- **Conversa** por texto/voz com LLM local via [Ollama](https://ollama.com) (padrão: `qwen3:8b`)
- **Ferramentas com segurança**: shell local, SSH para hosts confiáveis, desktop e browser do próprio usuário — sempre com schema, classificação de risco e aprovação single-use para ações sensíveis
- **Voz local**: STT com Faster-Whisper, TTS com fallback (kokoro → edge-tts → pyttsx3), always-listening com wake word
- **Homelab**: control plane com probes ICMP/TCP/SSH, adapters OpenWrt/Proxmox/HA, inventário somente-leitura
- **Operations UI**: painel web embutido no app desktop (capabilities, integrações, rede, voz, settings)
- **Credential Broker**: segredos ficam no Windows Credential Manager; o LLM nunca vê valores
- **Estados honestos**: `UNCONFIGURED`/`AUTH_FAILED`/`OFFLINE` reais — nada de status inventado

## Arquitetura

```text
NYRA Desktop (Tauri/Rust)  ── janela de presença + painel + bandeja
        └── Frontend React/Vite (embutido no release)
                └── Backend FastAPI (127.0.0.1:8000)
                        ├── Ollama (127.0.0.1:11434, externo)
                        ├── Tools/Agent (system_shell, remote_shell SSH, approvals)
                        ├── Voz (STT/TTS locais)
                        ├── Local Operator (desktop/browser/jobs/workflows)
                        └── Homelab & Integrações (OpenWrt, Proxmox, HA, Sentinel)
```

## Requisitos

- Windows 10/11 x64
- Python 3.11
- Node.js 18+ (frontend)
- Rust + Tauri CLI (apenas para o app desktop)
- [Ollama](https://ollama.com)

## Rodar em desenvolvimento

```powershell
# backend
python -m venv .venv
.venv\Scripts\pip install -r backend\requirements.txt
copy .env.example .env   # ajuste se quiser
cd backend
..\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# frontend (outro terminal, na raiz)
cd frontend
npm install
npm run dev            # http://127.0.0.1:5173

# app desktop (opcional, terceiro terminal)
cd desktop
npm install
npm run dev
```

Health check: `http://127.0.0.1:8000/health`

## Build release

```powershell
cd frontend && npm run build && cd ..
cd desktop && npm run build    # gera NSIS em src-tauri\target\release\bundle\nsis
```

O instalador inclui um `nyra-backend.exe` standalone (PyInstaller, gerado a
partir de `backend/run_backend.py`) e o app inicia o backend sozinho ao abrir.

## Segurança (resumo)

- Secrets apenas no Credential Broker (`NYRA_CRED:*` no Credential Manager, fallback DPAPI)
- Nenhum texto gerado pelo LLM executa nada diretamente; tools têm schemas Pydantic + risco + approval gate
- Shell local classifica cada comando (`READ_ONLY` … `CRITICAL`) com timeout, redaction e auditoria
- SSH só para hosts do registry local (`config/homelab_hosts.yaml`, não distribuído — crie o seu)
- Sem bypass de UAC; elevação é sempre legítima e aprovada

Leitura recomendada: [`docs/Manual_NYRA.md`](docs/Manual_NYRA.md) · [`AGENTS.md`](AGENTS.md) (regras permanentes do projeto)

## Status

Em uso diário pela própria pessoa que desenvolve. Funcionalidades mudam com
frequência; não há API estável nem promessas de suporte.

## Licença

[MIT](LICENSE)
