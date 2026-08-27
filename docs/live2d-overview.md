# NYRA Live2D V5

Fluxo implementado:

```text
Attention/Reaction -> AvatarController -> VTubeStudioAvatarProvider
 -> VTube Studio Public API -> parâmetros descobertos -> modelo Live2D
```

`AUTO` usa Live2D quando conectado/autenticado/modelo carregado e mantém o renderer atual como fallback. `LIVE2D` solicita o provider explicitamente; `CURRENT` desliga a injeção. O fechamento do VTS não afeta conversa, STT, TTS, Sentinel ou Network Watch.

Estado atual: bridge pronta, VTube Studio detectado, arte oficial `WAITING_FOR_LAYERED_ART`. Não se declara NYRA Live2D concluída antes do PSD, rig e export reais.
