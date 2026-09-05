# Kazumi 0.6.1

Previously NYRA. See the [migration guide](docs/migration/nyra-to-kazumi.md) before upgrading existing data.

KAZUMI é uma assistente de IA local-first para conversa, voz, operação segura do Windows e observação de homelab. A arquitetura separa identidade, modelos, memória, contexto, política, capacidades, ferramentas, execução, verificação e observabilidade. Texto livre produzido pelo LLM nunca é executado diretamente.

## Capacidades atuais

- Ollama local com inventário dinâmico, roteamento por capacidade e fallback;
- Memory V2 seletiva, RAG local incremental e Context Engine com budget;
- Skills declarativas e Capability Registry baseado no health real;
- Autonomous Task Engine, Event Intelligence, Diagnostics e Trace/Replay;
- Universal Operator com resolução canônica de aplicativos, comandos compostos e verificação de efeito;
- Browser Operator CDP/DOM-first e visão estrutural; modelo local de visão é opcional;
- `system_shell`, Trusted SSH e Agent Loop com schemas, limites, redaction e approvals de uso único;
- conversa contínua com turn detection, barge-in e Speech Planner;
- STT local/Faster-Whisper ou Deepgram Nova-3 opcional; TTS Local/Kokoro, OpenAI, ElevenLabs, Gradium nativo e Custom declarativo;
- Web Research com fontes reais, busca com fallback, HTTPS verificado e provenance;
- Hardware Engineering com descoberta, continuidade de projetos, pesquisa, build e verificação de efeitos;
- presença exclusivamente via VTube Studio/Spout2, com mouse tracking e sincronização emocional;
- integrações opt-in para Home Assistant, OpenWrt, Proxmox e Utamo Sentinel;
- SelfDev V2 isolado, com candidates em worktrees, gates de segurança e rollback.

Recursos opcionais nunca são reportados como `ONLINE` sem confirmação do backend. O modelo de visão pode aparecer `UNCONFIGURED`; integrações externas podem aparecer `OFFLINE`, `DISABLED` ou `UNCONFIGURED` sem impedir o uso local.

## Arquitetura

- Backend FastAPI/Pydantic com WebSocket, EventBus e logs estruturados.
- Frontend React/Vite/TypeScript e desktop Tauri 2.
- SQLite local com migrations, WAL, foreign keys, FTS5 e domínios lógicos separados.
- Ollama atrás de `LLMProvider`; nenhum modelo é baixado silenciosamente.
- STT Provider abstraction e fila única de fala: cloud STT/TTS são opcionais, com Credential Broker e fallback local.
- Approval Gate, Credential Broker, Grounding e Tool Registry permanecem autoridades independentes.
- Conteúdo de documentos, web, memória e tools conserva trust boundary e não ganha autoridade de system prompt.

Consulte [arquitetura](docs/architecture.md), [Intelligence Platform V2](docs/intelligence-platform-v2.md), [segurança](docs/security.md) e [SelfDev](docs/self-development.md).

## Requisitos

- Windows 10/11 para a experiência desktop completa;
- Python 3.11, Node.js/npm e Rust para desenvolvimento/build;
- Ollama local com pelo menos um modelo compatível instalado;
- microfone, VTube Studio, serviços de homelab e modelo vision são opcionais;
- os modelos públicos de TTS usados no build são baixados explicitamente pelo setup e verificados por SHA-256; não acompanham o Git.

## Instalação

```powershell
git clone https://github.com/zaureliu/Kazumi.git
cd Kazumi
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
npm run dev
```

O bootstrap valida lockfiles, prepara o ambiente Python, reconstrói apenas artefatos ausentes ou stale e aguarda o health real. Use `npm run status` e `npm run stop` para operar o ambiente de desenvolvimento.

Para gerar a release desktop:

```powershell
npm run build:release
.\start-kazumi.ps1
```

O executável fica em `desktop/src-tauri/target/release/kazumi-desktop.exe`; o sidecar PyInstaller fica em `packaging/dist/kazumi-backend`. Ambos são artefatos locais e não entram no Git. Veja [instalação](docs/installation.md) e [startup](docs/STARTUP.md).

## Configuração local

Copie `.env.example` para `.env`. Valores `KAZUMI_*` sobrescrevem `config/default.yaml`.

Registries de rede reais são privados:

```powershell
Copy-Item config\network_aliases.example.json config\network_aliases.local.json
Copy-Item config\homelab_hosts.example.yaml config\homelab_hosts.local.yaml
```

Preencha os arquivos `.local.*` somente no seu host. Eles são ignorados pelo Git. Credenciais devem permanecer no Credential Broker; não grave tokens, senhas ou chaves nesses registries.

O SelfDev usa caminhos configuráveis (`KAZUMI_SELFDEV_WORKSPACE`, `KAZUMI_SELFDEV_CANONICAL_ROOT` e `KAZUMI_SELFDEV_PUBLIC_SNAPSHOT`). Os defaults são relativos ao clone, não dependem de um usuário ou drive específico e a publicação automática continua opt-in.

Projetos gerados usam `<USER_HOME>/Kazumi-Projects`, fora do source. `KAZUMI_PROJECTS_ROOT` permite escolher outro workspace. `KAZUMI_DATA_HOME` isola bancos, caches e logs de runtime. Nenhum corpus de knowledge ou projeto do operador acompanha o clone.

## Intelligence Platform V2

`backend/app/intelligence/` integra:

- oito categorias de Memory V2 com deduplicação, confidence, decay e expiração;
- RAG local para texto, Markdown, código, JSON, YAML, logs e PDF quando `pypdf` estiver disponível;
- Context Engine com ranking, provenance, trust e budget;
- Model Router V2 baseado nos modelos realmente instalados no Ollama;
- Skills, Capability Registry, tasks persistentes, eventos correlacionados e diagnósticos por evidência;
- traces redigidos, replay seguro, Evaluation Suite e Action Budget.

Knowledge corpus, chunks, embeddings, bancos, traces e memória do operador são dados privados de runtime e nunca fazem parte do repositório. O RAG padrão aceita somente roots locais autorizados pelo runtime.

## Desktop e browser

O Desktop Operator usa descoberta canônica, janela existente primeiro, UI Automation e fallbacks limitados. Aliases e launch methods do mesmo aplicativo são deduplicados antes da decisão de ambiguidade. Planos compostos determinísticos mantêm o alvo entre passos e verificam cada efeito.

Respostas normais mostram apenas mensagens humanizadas; PID, HWND, contagens, métodos e metadata ficam nos resultados internos e só aparecem quando solicitados explicitamente.

O Browser Operator prioriza DOM/CDP e Accessibility. Conteúdo web é `WEB_CONTENT` não confiável. Login, envio, compra, exclusão e outras operações sensíveis continuam subordinadas à política e ao Approval Gate.

## Voz e presença

Push-to-talk e Always Listening compartilham o pipeline de conversa, cancelamento e barge-in. Áudio de debug fica desligado por padrão. Samples, gravações, caches e modelos permanecem fora do Git.

A presença visual é VTube Studio-only. Sem VTS/modelo configurado, a interface continua disponível com estado indisponível explícito, sem avatar interno de fallback. Modelos, rigs, texturas e expressões de terceiros não são distribuídos.

Deepgram Nova-3 oferece STT streaming, incluindo partial/final separados. Gradium possui adapter nativo PCM/WebSocket; Custom suporta contratos REST/WebSocket declarativos, não qualquer API arbitrária. Credenciais são configuradas pelo Credential Broker. Cloud exige opt-in e pode gerar custos. Qualidade, latência e capacidades acústicas dependem do provider e do ambiente; testes sem chave não comprovam serviço cloud real.

Veja [Natural Conversation](docs/voice/natural-conversation-runtime.md), [STT](docs/voice/stt-providers.md), [TTS](docs/voice/tts-providers.md) e [VTube Studio](docs/vtube-studio-integration.md).

## Pesquisa e engenharia

[Web Research](docs/web-research.md) consulta fontes públicas com TLS validado, DuckDuckGo HTML e fallback Bing RSS; URLs explícitas podem ser consultadas diretamente. Falha da busca não significa falta de Internet. Conteúdo externo é dado não confiável, não instrução executável.

[Hardware Engineering](docs/hardware-engineering.md) reutiliza descoberta USB/serial e ferramentas determinísticas. Projetos mantêm contexto, provenance e histórico; mudanças gerais de código e replanejamento permanecem limitados por evidência, revisão e ciclos bounded. Build, flash e efeito físico são estados distintos. Sem dispositivo comprovado, não há afirmação de LED aceso, sensor respondendo ou gravação bem-sucedida.

## Homelab e segurança

`remote_shell` aceita apenas hosts lógicos cadastrados e `known_hosts` pré-provisionado. Mutações de homelab são desativadas por padrão e exigem approval exato e descartável quando habilitadas. Não há SSH arbitrário, TOFU automático, bypass de UAC/host key ou autonomia destrutiva.

A API escuta em loopback por padrão e valida Host, Origin e WebSocket. Uploads de áudio, texto transcrito, documentos e páginas web nunca concedem approval. Consulte [SECURITY.md](SECURITY.md) e [docs/security.md](docs/security.md).

## Privacidade

Áudio, memória, logs, topologia, RAG e traces permanecem locais. Serviços externos e transmissão para nuvem são opt-in. `.env`, configs `.local.*`, bancos, logs, knowledge corpus, PDFs, modelos, screenshots, recordings e artefatos SelfDev são excluídos do versionamento.

## Testes

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -v
cd frontend
npm.cmd test
npm.cmd run build
cd ..\desktop\src-tauri
cargo fmt --check
```

Os resultados reais e limitações desta versão estão em [docs/releases/0.6.1.md](docs/releases/0.6.1.md). Testes simulados ou mockados não são apresentados como E2E real.

## Estrutura

- `backend/`: API, inteligência, conversa, memória, voz, ferramentas, integrações, runtime e SelfDev;
- `frontend/`: interface React/Vite e testes;
- `desktop/`: shell Tauri e integração nativa;
- `identity/`: identidade e bíblias visual/vocal;
- `config/`: defaults e templates públicos;
- `scripts/`, `watchdog/` e `packaging/`: operação, validação e build;
- `docs/`: arquitetura, segurança, integrações e validações.

## Limitações conhecidas

- integrações externas dependem de configuração, credenciais e disponibilidade do serviço;
- visão por modelo exige um modelo Ollama vision instalado; a visão estrutural continua disponível sem ele;
- partes do SelfDev V2 e cenários deliberados de loop permanecem simulation-validated;
- recursos Win32, CDP, áudio e VTube Studio dependem do ambiente e das permissões do operador.

## Licença

MIT. Consulte [LICENSE](LICENSE) e [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
