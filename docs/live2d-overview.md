# VTube Studio Presence

Fluxo oficial:

```text
Persona/Avatar state -> VTubeStudioAvatarProvider -> VTube Studio Public API
VTube Studio current model -> Spout2 -> Desktop Presence
Windows GetCursorPos -> normalized virtual desktop -> VTS parameters
```

Desktop Presence usa exclusivamente o modelo atualmente carregado no VTube Studio. Uma troca de modelo é detectada pela API e recarrega parâmetros, hotkeys e expressions. VTS offline deixa a camada de personagem vazia; conversa, TTS e integrações continuam funcionando.

Não existe renderer Live2D embutido nem cópia local do modelo.
