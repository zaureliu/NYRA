# Integração VTube Studio

Configuração em **Settings > Desktop Presence**: enabled, host/porta loopback, auto-connect, lip sync, sender Spout2 e mouse tracking `OFF`, `EYES` ou `HEAD_EYES`.

O provider descobre o modelo atual, parâmetros, hotkeys e expressions pela API oficial. A janela nativa recebe o sender `VTubeStudioSpout` por Spout2, preservando alpha, always-on-top, click-through, drag e reconnect. Screenshot/captura de janela não são usados.

O arquivo local de settings migra qualquer renderer antigo para `VTUBE_STUDIO`. Não há seletor ou fallback de renderer interno.
