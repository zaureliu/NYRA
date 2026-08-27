# Validação NYRA V3 — 2026-08-19

## Linha de base antes das alterações

- branch: feature/voice-desktop-presence; worktree limpo;
- backend: 17/17 testes;
- frontend: 1/1 teste e build;
- desktop: cargo check;
- runtime: backend, frontend e desktop ativos; health online;
- avisos existentes: depreciação Starlette/httpx e canonicalização Cargo de `<NYRA_ROOT>`.

Branch criada: feature/v3-definitive-character.

## Resultado automatizado

- backend: 22/22;
- frontend: 6/6 em quatro arquivos;
- frontend TypeScript/Vite: build aprovado;
- Rust: cargo check aprovado;
- Tauri release + NSIS: build aprovado.

Cobertura nova: manifest, assets essenciais, alpha PNG, fallback de manifest, seleção de renderer, mapeamento de estado visual, transparência Tauri, perfil oficial, parâmetros reais, pronúncia e prosódia.

## Visual/Desktop

- renderer ativo: LayeredRenderer; PNGRenderer e SVGRenderer disponíveis; FutureLive2D reservado;
- Desktop Full e Portrait: 1024×1536 RGBA, alpha 0 nos cantos;
- V2 preservado como fallback;
- causa do quadrado V2: rect de fundo e camada decorativa dentro de nyra.svg;
- correção V3: asset sem fundo + roots CSS transparentes + Tauri transparent/frameless/shadowless;
- confirmação: nenhum card, quadrado, moldura ou background permanente no Desktop;
- click-through: hit-test passou com Ctrl+Alt+I fallback; o PID sob o corpo mudou de NYRA para o app abaixo;
- posição/tamanho: tauri-plugin-window-state; primeira posição usa work area do monitor; escalas 50/75/100/125/150%;
- interactive e tray preservados.

Capturas: [dashboard](screenshots/dashboard-v3.png), [overlay branco](screenshots/overlay-v3-white.png), [overlay preto](screenshots/overlay-v3-black.png), [overlay idle](screenshots/overlay-v3-current.png).

Teste sobre VS Code foi tentado, mas o Windows manteve a janela do editor em outro contexto de foreground/desktop e a captura não foi aceita como evidência; branco/preto e terminal passaram. Capturas individuais listening/thinking/speaking não foram retidas, mas os mapeamentos têm teste automatizado e os eventos reais mudaram o estado durante a conversa.

## Voz/STT/conversa

- providers: Kokoro funcional e Chatterbox funcional em CPU; Kokoro é fallback;
- voz oficial: pf_dora, única feminina nativa pt-BR no pack local;
- Chatterbox health cold-start passou após timeout local ser ampliado de 30 para 90 s;
- conversa livre passou sem comandos especiais;
- resposta curta de VLAN passou a uma frase;
- filtro remove closers de atendente adicionais;
- memória Orion persistiu após conversa intermediária e reinício;
- VAD Silero detectou toda a fala dos benchmarks;
- faster-whisper: 0,36 s Kokoro e 0,29 s Chatterbox na frase casual;
- nenhum começo/final foi cortado nas duas amostras; clipping false;
- teste físico de microfone requer clique/permissão do operador e não foi acionado silenciosamente.

Latência quente observada: LLM 0,75–2,16 s; Kokoro 1,48 s em frase mínima e 11,61 s em explicação longa. Cold start isolado do primeiro turno: 91,59 s ponta a ponta.

## Desempenho

Release Tauri + árvore WebView2 estabilizada: 1,849% CPU total do host, 366,6 MB working set e 201,8 MB private em idle. Turno falado: 2,232% CPU médio, 397,6 MB working set e 226,2 MB private. Processo nativo isolado ficou em cerca de 25 MB; os valores maiores incluem oito/nove processos WebView2. Backend com modelos carregados: cerca de 585,5 MB working set e 965,5 MB private na amostra.

## Erros encontrados e correções

1. SVG V2 continha fundo retangular: V3 usa RGBA real.
2. Portrait inicial veio RGB/checkerboard: rejeitado e extraído novamente.
3. Blink provisório criou arcos rosados: removido e recalibrado.
4. Chatterbox recebeu falso negativo de health: timeout 90 s.
5. start.ps1 rastreava npm.cmd e deixava Vite órfão: agora inicia/rastreia node.exe.
6. Filtro de resposta não cobria “testar ou verificar”: corrigido.
7. Voice Lab misturava nomes sentence_pause sem sufixo: contrato alinhado em _ms.

## Limitações

- arte ainda não é rig segmentado profissional/Live2D;
- expressão facial sobre PNG usa overlays substituíveis;
- sem áudio de referência autorizado, Chatterbox padrão não é a identidade oficial;
- microfone físico e julgamento auditivo humano dependem do operador;
- Chatterbox CPU tem latência alta;
- screenshot específico sobre VS Code não foi validado.

## Inicialização

powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1

powershell -ExecutionPolicy Bypass -File .\scripts\start-desktop.ps1

Status: powershell -ExecutionPolicy Bypass -File .\scripts\status.ps1

Parar: powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1
