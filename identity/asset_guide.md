# Guia de assets — NYRA Avatar V2 (ativo)

Pack ativo: `frontend/public/avatar/nyra_v2/`.

- `avatar-manifest.json`: canvas, árvore, landmarks, assets, blink e estados;
- `master/nyra-avatar-master.png`: master RGBA servida pelo frontend;
- `eyes/open.svg`, `half.svg`, `closed.svg`: layers full-canvas;
- `mouth/closed.svg`, `small.svg`, `medium.svg`, `open.svg`: layers full-canvas;
- `frontend/src/assets/nyra-v2/master/nyra-avatar-master.png`: master interna oficial e imutável para derivações;
- `docs/avatar-v2.md`: contrato completo e procedimento de extensão.

Todo layer facial mede `1086×1448` e usa `viewBox="0 0 1086 1448"`. Não criar crops locais nem offsets por viewport. A referência original em `E:\nyra-v2\nyra_master.png` permanece fora do projeto e read-only.

## Histórico legado — assets V3

Pack: frontend/public/avatar/nyra_v3/.

- bust/nyra-bust-violet.png: 1024×1120 RGBA, visual oficial do Desktop Presence;
- portrait/nyra-portrait-violet-rgba.png: 1024×1536 RGBA, dashboard;
- desktop/nyra-full-violet-rgba.png: 1024×1536 RGBA, Full Body opcional/fallback;
- manifest.json: contrato de variantes, estados, escala, âncora e fallback;
- expressions/, eyes/, mouth/: mapas de camadas substituíveis;
- layers/: ordem de segmentação futura;
- symbols/nyra-symbol.svg: marca vetorial;
- fallback/: documentação do V2 preservado.

Assets novos devem manter cabelo base `#24162F`, sombras `#160F20`, meios-tons/reflexos `#3A214A`/`#59356C`/`#65417A`, mecha teal discreta, olhos teal, Neural Link ciano e roupa grafite em todos os estados. Não reintroduzir castanho/preto neutro ou roxo neon.

Exigir alpha real, sem checkerboard, cenário, sombra retangular ou padding excessivo. Antes de integrar, validar PNG color type 4/6, pixels dos quatro cantos com alpha 0 e compositar sobre branco e `#18181B`. `scripts/avatar-alpha-extract.py` existe somente para corrigir matte claro dos assets aprovados; geração futura deve preferir RGBA nativo. Framing, asset, âncora e coordenadas faciais pertencem ao `manifest.json`, não a componentes dispersos.
