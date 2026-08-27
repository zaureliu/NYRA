# NYRA 0.3.0

NYRA é uma assistente de IA local para homelab construída como **identidade + LLM + memória + percepção + voz + avatar + ferramentas + eventos**. Ela sabe que é uma IA, mantém personalidade persistente e observa infraestrutura sem tomar decisões administrativas pelo operador.

## Objetivo

Oferecer uma assistente pessoal local-first, auditável e extensível para conversa, voz, automação segura do computador e observação de homelab, mantendo dados privados no host e integrações externas sempre opt-in.

## Principais recursos

- chat e voz locais com Ollama, STT e TTS desacoplados;
- memória seletiva, identidade persistente e EventBus tipado;
- avatar web e presença desktop via Tauri;
- Universal Operator com approvals de uso único, verificação de efeito e recuperação;
- integrações read-only por padrão para homelab e Utamo Sentinel;
- Self-Development Engine com candidates isolados, testes, rollback e publicação sanitizada.

## Arquitetura

- FastAPI + WebSocket, configuração Pydantic/YAML/.env e logs JSON.
- Ollama atrás de `LLMProvider`; modelo inicial `qwen3:8b` já existente.
- SQLite com tabelas separadas e FTS5, retenção e contexto seletivo.
- faster-whisper local para português, com Web Audio, medidor, normalização e Silero VAD ONNX.
- `ConversationEngine`: turn detection, STT reutilizável, estados explícitos, barge-in speech-only e telemetria de latência.
- `TTSProvider`: Kokoro ONNX/`pf_dora` local como primário e Windows SAPI como fallback; Chatterbox permanece somente experimental.
- React/Vite/TypeScript multipágina, WebSocket, Avatar V2 aprovado com master imutável, camadas SVG, lip sync e fallback estático; Tauri 2 oferece presença transparente e mouse follow global no desktop.
- Shell local arbitrário classificado/auditado, Agent Loop limitado e SSH somente para hosts cadastrados.
- Self-Development Engine local com evidência, fila persistente, worktrees isolados, testes selecionados, scan de segurança, promoção reversível e publicação externa opt-in.

Detalhes em [docs/architecture.md](docs/architecture.md) e [docs/self-development.md](docs/self-development.md).

A Conversation Engine V2 acrescenta streaming casual real, TTS incremental por sentença, interrupção sem cancelar automaticamente a tarefa e preload/recovery do Ollama. A tela normal de áudio expõe somente preferências com efeito no runtime; diagnósticos mostram STT, TTS, readiness, TTFT e TTFA. Consulte [arquitetura V2](docs/conversation-engine-v2.md) e [auditoria dos controles](docs/audio-control-audit.md).

A V5 adiciona `Settings > AI > Brain Lab` para comparar `qwen3:8b` e `qwen3.5:9b` sem troca automática, e `Settings > Visual > Live2D` para a bridge oficial do VTube Studio. O Avatar V2 permanece a identidade visível oficial; a arte NYRA Live2D está marcada `WAITING_FOR_LAYERED_ART`. Veja [Brain](docs/ollama-brain.md), [benchmark](docs/brain-benchmark.md) e [Live2D](docs/live2d-overview.md).

Resultados executados estão em [docs/validation.md](docs/validation.md) e os binários em [docs/model-inventory.md](docs/model-inventory.md).

## Universal Operator e sete camadas de autonomia

O controle local é dividido em percepção, estado do computador, entendimento de intenção, Universal Operator, verificação de efeito, aprendizado de uso e memória de skills. Texto livre do LLM nunca é executado diretamente: shell local passa por `system_shell`, SSH por `remote_shell` e ações elevadas exigem approval exato e descartável.

## Self-Development Engine

O SelfDev observa métricas e eventos, exige evidência e hipótese de causa, cria worktrees em um workspace configurável fora da raiz canônica, seleciona testes, compara o candidato e só promove mudanças permitidas pela política de risco. O modo padrão é `AUTONOMOUS_SAFE`; publicação automática permanece desligada durante o bootstrap da versão 0.3.0. Detalhes em [docs/self-development.md](docs/self-development.md).

## Satélite de voz

O WebSocket local implementa o contrato `nyra.voice.v1`, com handshake, heartbeat, barge-in e cancelamento isolado de TTS. Qualquer exposição para outro dispositivo exige configuração explícita e não altera o padrão loopback/local-first.

## Requisitos

- Windows 10/11 para a experiência desktop completa;
- Python 3.11, Node.js/npm e Rust somente para compilar o desktop;
- Ollama instalado localmente e ao menos um modelo compatível já baixado;
- microfone opcional para voz e VTube Studio opcional para avatar externo.

## Instalação e início

```powershell
cd .\NYRA
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
npm run dev
```

`npm run dev` é o entrypoint oficial e idempotente: valida os dois `package-lock.json`, reutiliza dependências saudáveis, reconstrói apenas artefatos ausentes ou stale, inicia backend/Vite/Tauri em ordem e aguarda `/api/health`. Abra `http://127.0.0.1:5173`, consulte com `npm run status` e pare com `npm run stop`. Instruções completas: [docs/installation.md](docs/installation.md).

Para a personagem flutuante independente do navegador:

```powershell
npm run build:release
.\start-nyra.ps1
```

Detalhes e atalhos: [docs/desktop-presence.md](docs/desktop-presence.md).

## Configuração

Copie `.env.example` para `.env`; o setup faz isso se necessário. Valores `NYRA_*` sobrescrevem `config/default.yaml`. Portas padrão: frontend 5173, backend 8000, Ollama 11434. Credenciais de integrações configuradas pela interface ficam no Credential Broker do Windows; `.env` é apenas uma fonte local legada e nunca deve ser versionado.

## Ollama

O backend sobe sem bloquear e o `OllamaWarmManager` faz preload isolado, warm-up opcional, keep-alive, recovery e readiness confirmada por `/api/ps`. Neste host/Ollama 0.32.15 o default validado é `NYRA_OLLAMA_KEEP_ALIVE=1h`. Troque `NYRA_OLLAMA_URL`, `NYRA_LLM_MODEL` ou implemente outro `LLMProvider`; warm-up não entra no chat, memória, TTS ou tools.

Modelos instalados podem ser selecionados na interface sem download automático. O SelfDev usa `qwen3:8b` por padrão e restringe a autopromoção conforme complexidade e risco.

## Voz e microfone

Push-to-talk e Always Listening usam o mesmo pipeline. A captura aplica AEC/NS/AGC quando disponíveis, turn detection com pausa natural, Silero VAD e Faster-Whisper carregado uma vez. Durante a fala da NYRA, barge-in usa threshold reforçado para reduzir self-listening e interrompe apenas o TTS. A tela **Settings > Áudio e conversa** contém microfone, speaker, voz efetiva, velocidade, volume, modo, Always Listening e interrupção, além de testes reais de microfone e voz.

## Memória

Categorias: short-term, episodic, semantic, preferences e homelab_events. API permite criar, pesquisar, listar, excluir e mudar importância. Memória recente e eventos antigos de baixa importância são podados; fatos estáveis não são apagados automaticamente.

## Avatar V2

O pack `frontend/public/avatar/nyra_v2/` usa a master aprovada como visual oficial no dashboard e no Desktop Presence. Camadas no mesmo canvas fornecem olhar em 13 direções, blink suave, expressões e seis estados de boca sem redesenhar a personagem. O renderer respeita reduced motion, faz preload dos assets e usa mouse follow local na web e global via Tauri/Win32 no Desktop Presence. O pack V3 permanece apenas como histórico/rollback e não é selecionável pela interface.

## Homelab e segurança

A NYRA pode usar `system_shell` local e `remote_shell` somente em hosts lógicos confiáveis. O SSH exige `known_hosts` pré-provisionado inclusive com senha. Mutações de homelab ficam desativadas por padrão e, quando habilitadas pelo operador, todas exigem approval de uso único ligado ao payload exato. Não há SSH arbitrário, TOFU automático, bypass de UAC/host key ou autonomia destrutiva.

A API local rejeita `Host`, `Origin` e WebSocket de origens não autorizadas. Uploads de áudio e Always Listening nunca concedem approvals. Criação arbitrária de processos/jobs não é exposta ao LLM, tarefas/workflows nem à API; scripts de navegador exigem approval ligado ao hash, aba e URL completa. Credenciais de Home Assistant/Proxmox são invalidadas quando sua origem muda. Consulte [docs/security.md](docs/security.md).

## Privacidade

Áudio, memória, logs e topologia permanecem locais. Nenhum serviço cloud é configurado. A UI e API escutam apenas em `127.0.0.1`; credenciais só trafegam por HTTPS ou, em HTTP, para um IP loopback literal.

## Utamo Sentinel Bridge

A integração opcional com Utamo Sentinel reutiliza Socket.IO para discovery autenticado, eventos em tempo real, histórico local e alertas de voz. Ela é read-only e fica OFF por padrão. Configure em `Settings > Integrations > Utamo Sentinel`; consulte [docs/integrations/utamo-sentinel.md](docs/integrations/utamo-sentinel.md) e [docs/sentinel-security.md](docs/sentinel-security.md).

## Testes

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -v
cd frontend
npm.cmd test
npm.cmd run build
```

## Estrutura do projeto

- `backend/`: API, conversa, memória, voz, ferramentas, integrações, runtime e SelfDev;
- `frontend/`: interface React/Vite e testes de componentes;
- `desktop/`: shell Tauri e integração Win32;
- `identity/`: identidade, personalidade e bíblias visual/vocal;
- `config/`: defaults e schemas de serviços;
- `scripts/`, `watchdog/` e `packaging/`: operação, validação e empacotamento;
- `docs/`: arquitetura, segurança, integrações e evidências de validação.

## Solução de problemas

- `npm.ps1 não pode ser carregado`: use `npm.cmd` ou os scripts fornecidos.
- LLM falso no health: execute `ollama serve`, confirme `ollama list` e `qwen3:8b`.
- Primeira transcrição lenta: o modelo tiny é carregado na primeira captura; o setup faz preload.
- TTS indisponível: confirme que uma voz SAPI está instalada e teste o health. Voz pt-BR depende do pacote de idioma do Windows.
- Microfone sem rótulo: conceda permissão ao navegador e recarregue.
- Portas ocupadas: pare processos anteriores ou ajuste `.env`, scripts e proxy Vite de forma consistente.
- Logs: `%LOCALAPPDATA%\NYRA\logs` contém `application.log`, `conversation.log`, `tools.log`, `homelab.log`, `voice.log`, `microphone.log` e `errors.log`; o Tauri também grava no diretório local da aplicação.
O `Pronunciation Lab` permite revisar termos técnicos PT-BR, comparar original/corrigido e salvar aliases sem editar JSON ou reiniciar a aplicação. Consulte `docs/pronunciation-engine.md`.

## Escuta contínua e Network Watch

Hands On/Always Listening é local e inicia ativo em modo Hands-Free; Network Watch continua opt-in. O overlay exibirá `MIC ON` enquanto a captura estiver ativa e um fallback discreto quando não houver entrada ou permissão.

Atalhos Desktop Presence: `Ctrl+Shift+Space` para push-to-talk, `Ctrl+Shift+M` para mute/unmute, `Ctrl+Shift+N` para mostrar/ocultar e `Ctrl+Shift+I` para click-through.

Documentação: [Always Listening](docs/always-listening.md), [Wake Word](docs/wake-word.md), [Hands-Free](docs/hands-free.md), [Network Watch](docs/network-watch.md), [Alertas proativos](docs/proactive-alerts.md) e [Privacidade](docs/privacy.md).

## Limitações

- Ollama e modelos de voz são dependências locais separadas e podem deixar o health degradado quando indisponíveis.
- Recursos Win32, captura visual, áudio e integrações reais dependem do ambiente e das permissões do operador.
- Arte Live2D totalmente rigada continua dependente de assets externos; o Avatar V2 permanece o fallback oficial.

## Roadmap

- ampliar adapters locais sem enfraquecer approvals e grounding;
- evoluir métricas e candidates LOW_RISK do SelfDev;
- melhorar portabilidade dos fluxos desktop e de voz mantendo cloud opt-in.

## Licença

Distribuído sob a licença MIT. Consulte [LICENSE](LICENSE).
