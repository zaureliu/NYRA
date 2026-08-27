# NYRA Avatar V2

## Fontes de verdade

- Referência visual versionada: `frontend/src/assets/nyra-v2/master/nyra-avatar-master.png`.
- SHA-256 observado antes da implementação: `50E7BD5E8A24D852D974EAD6CE8DCDE5EBF4488EEF2CEE8A4AB9DBF9B834822A`.
- Master interna oficial: `frontend/src/assets/nyra-v2/master/nyra-avatar-master.png`.
- Cópia servida pelo Vite: `frontend/public/avatar/nyra_v2/master/nyra-avatar-master.png`.
- Implementação e assets derivados pertencem somente a este repositório. A referência em `E:` é read-only e nunca deve ser sobrescrita.

## Identidade e arte oficial

A personagem ativa preserva da referência o rosto oval delicado, mandíbula e queixo finos, olhos grandes azul-turquesa, sobrancelhas suaves, cabelo longo loiro-mel com franja dividida, aparência adulta e expressão calma/acolhedora. A arte interna é chest-up, com blusa creme de gola delicada, cardigan vinho/ameixa e headphones over-ear graphite/dark navy com acentos mínimos em violeta/ciano.

A geração usou a referência original como edit target de preservação de identidade. Somente roupa, headphones, enquadramento e fundo foram alterados. A ferramenta integrada retornou matte claro RGB; o extrator local e limitado `scripts/avatar-alpha-extract.py` converteu apenas o componente claro conectado às bordas em alpha. A master final é PNG RGBA `1086×1448`, com alpha 0 nos quatro cantos.

## Por que o renderer antigo desalinhava

O V3 mostrava um PNG completo com olhos e boca já pintados e adicionava linhas/ellipses SVG por cima. O bitmap usava `object-fit: contain`, enquanto o overlay usava `viewBox="0 0 100 100"` e `preserveAspectRatio="none"`. Bust, portrait e full-body tinham landmarks percentuais diferentes. Além disso, `AvatarControl` transformava somente `.nyra-avatar-base`; eyes, mouth e Neural Link ficavam em outro layer. O blink era uma opacidade CSS fixa de 6,4 s, sem estados reais, e os overlays não ocultavam de forma artística os olhos/boca originais. Isso produzia:

- alongamento independente conforme o aspect ratio do container;
- olhos/boca duplicados e efeito de sticker;
- offsets diferentes por framing;
- base movendo sem acompanhar os overlays;
- cadência de blink previsível e sem `OPEN → HALF → CLOSED → HALF → OPEN`.

## Coordinate system único

`frontend/public/avatar/nyra_v2/avatar-manifest.json` é a única fonte de verdade geométrica.

| Elemento | Landmark/bounds no canvas |
| --- | --- |
| Canvas | `1086 × 1448`, viewBox `0 0 1086 1448` |
| Character root | origem `(543, 1448)` |
| Head | centro `(522, 465)`, transform origin `(522, 610)` |
| Left eye | centro/anchor `(390, 500)` |
| Right eye | centro/anchor `(653, 495)` |
| Mouth | centro/anchor `(522, 675)` |
| Left earcup | centro `(219, 494)` |
| Right earcup | centro `(866, 484)` |

Todos os SVGs de eye e mouth declaram `width="1086" height="1448" viewBox="0 0 1086 1448"`. Nenhum breakpoint possui offsets faciais. Desktop, dashboard, tablet e mobile escalam o mesmo `<svg preserveAspectRatio="xMidYMid meet">`.

## Árvore de renderização

```text
NYRA CHARACTER ROOT (um único SVG)
├── BASE / MASTER RGBA
├── BODY
│   └── breathing highlight (somente peito/roupa)
└── HEAD
    ├── FACE
    │   ├── EYES (half e closed; open vem da master)
    │   └── MOUTH (small, medium e open; closed vem da master)
    └── HEADPHONES
        └── indicador funcional sutil
```

Os headphones físicos fazem parte da master interna, portanto não podem flutuar, deslizar ou receber escala própria. Apenas o indicador luminoso pertence ao subgrupo `headphones`, filho de `head`. Backend control, resize e responsividade transformam `character-root` como unidade.

## Blink

`NyraAvatarV2Renderer` agenda um único `setTimeout` por vez, com cleanup. A sequência é `open → half → closed → half → open`; o total configurado é 192 ms e o intervalo aleatório fica entre 3,6 e 7,2 s. Left/right eyes são um único state layer, portanto mudam no mesmo frame e conservam seus anchors. `prefers-reduced-motion` desativa blink automático, mas estados funcionais explícitos continuam renderizando.

## Mouth e lip sync

`useAudioLipSync` usa WebAudio `AnalyserNode`, smoothing assimétrico e `requestAnimationFrame`. Os thresholds centralizados em `avatar/lipSync.ts` mapeiam amplitude para `closed`, `small`, `medium` e `open`. `AudioContext`, áudio e RAF têm cleanup em stop, troca de áudio e unmount.

Quando existe `control.mouth_open`, ele tem prioridade durante `SPEAKING`. Quando há somente speaking state e nenhuma amplitude, o renderer usa uma sequência variável `small → medium → small → open → medium → small → closed` com durações diferentes. Silêncio/saída de speaking retorna a closed.

## Estados funcionais

- `IDLE`: olhos abertos, blink natural, respiração por highlight restrito ao body, headphones em repouso.
- `LISTENING` e `INTERRUPTED`: olhos abertos e indicador steady nos earcups.
- `UNDERSTANDING` e `THINKING`: expressão calma, leve ajuste de contraste e pulso lento do indicador.
- `SPEAKING`: lip sync por amplitude/controle ou fallback variável; blink continua; o headphone físico permanece imóvel.
- `OFFLINE`: eyes half e indicador apagado.

## Integração existente

`App.tsx` e `DesktopApp.tsx` continuam recebendo estados do WebSocket, push-to-talk, Always Listening, TTS tradicional e streaming. Ambos passam `status`, `mouth` e `AvatarControl` ao mesmo `AvatarRenderer`. A mudança visual não intercepta chat, STT, Sentinel ou Desktop Presence.

## Como adicionar um estado sem quebrar alinhamento

1. Derive a mudança da master interna e da referência original; não regenere a personagem inteira.
2. Crie somente o overlay necessário com canvas/viewBox `1086×1448` e transparência externa.
3. Não use `top`, `left`, viewport units ou media-query para eyes, mouth ou headphones.
4. Registre landmarks somente em `avatar-manifest.json`.
5. Renderize o novo layer dentro de `nyra-v2-character-root` e, para elementos da cabeça, dentro de `nyra-v2-head`.
6. Atualize `NyraAvatarV2Renderer.test.tsx` e execute `npm test`, `npm run test:avatar` e `npm run build`.
7. Regenere com `scripts/avatar-v2-contact-sheet.ps1` e inspecione `docs/screenshots/avatar-v2/nyra-avatar-v2-contact-sheet.png`.

## Validação visual

O smoke `frontend/scripts/avatar-v2-smoke.mjs` opera na rota integrada `/#dashboard`, valida 1920, 1366, tablet e 390 px, verifica overflow/viewBox/dimensões/parent de headphones e captura todos os estados. O smoke existente `frontend/scripts/ui-smoke.mjs` continua validando o chat real em `/#chat`.
