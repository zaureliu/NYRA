# Integração visual V3

## V3.4 — Bust/Portrait + cabelo violeta

O Desktop Presence usa `Character View = Bust / Portrait` como padrão. O crop termina abaixo do busto, preserva cabeça/cabelo, pescoço, ombros, tórax superior, olhos, boca e Neural Link. `Full Body` continua disponível no painel visual e no menu de configurações do Desktop.

O framing é resolvido em um único contrato (`manifest.json` + `avatar/framing.ts`), incluindo asset, âncora e coordenadas faciais. O tamanho-base Bust é 480×560; Full Body conserva 420×620. `Overlay Scale` permanece persistente e multiplica o tamanho próprio de cada framing.

Os três assets oficiais são RGBA, têm alpha 0 nos quatro cantos e compartilham cabelo violeta/ameixa, mecha teal, olhos teal, Neural Link ciano e roupa grafite. A janela Tauri permanece `transparent: true`, sem decoração/sombra de janela; `html`, `body`, `#root`, `.desktop-presence`, botão e avatar não possuem background. O único drop-shadow é orgânico e acompanha o alpha da silhueta.

Validações executadas:

- [composição sobre fundo claro](screenshots/nyra-bust-light.png);
- [composição sobre fundo escuro](screenshots/nyra-bust-dark.png);
- [captura real do Desktop Presence](screenshots/nyra-desktop-bust-live.png);
- estados idle/listening/thinking/speaking e expressões mapeados sem alterar asset/framing;
- cinco bocas preservadas para lip sync; playback mantém `SPEAKING` até o áudio terminar;
- blink suavizado para 6,4 s e idle/breathing mantido;
- build Tauri release e testes frontend executados.

AvatarRenderer carrega /avatar/nyra_v3/manifest.json e seleciona SVGRenderer, PNGRenderer ou LayeredRenderer. FutureLive2DRenderer está reservado no contrato e cai no V2 enquanto não houver modelo.

LayeredRenderer combina o PNG RGBA com overlays vetoriais de olhos, boca, topologia da íris e Neural Link. O hook de WebAudio continua dirigindo mouth_closed/small/medium/open. Expressões podem substituir a boca (amused/happy/surprised) fora da fala.

Falhas de manifest, asset essencial ou render geram console error e exibem /avatar/nyra.svg. A lógica emocional e operacional não pertence ao renderer.

O Desktop usa a variante corpo inteiro. html, body, #root, main e botão do avatar têm background transparente, sem pseudo-elemento, backdrop, border ou box-shadow retangular. O PNG tem alpha real.
