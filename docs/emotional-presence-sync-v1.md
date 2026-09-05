# Emotional Presence Synchronization V1

Persona Runtime continua sendo a única autoridade da emoção. O coordenador distribui o mesmo `emotion + intensity` para diálogo, voz e o adapter VTube Studio; nenhum adapter recalcula emoção.

O adapter de voz usa apenas capacidades nativas declaradas e preserva a identidade vocal. O adapter VTS descobre o modelo atual, parâmetros, hotkeys e expressions reais. O mapa emocional configurável fica no arquivo local `vtube-studio-settings.json`. Um alvo inexistente degrada para neutro sem inventar capability.

Desktop Presence é VTS-only. Sem VTube Studio ou sem frames Spout2 válidos, não renderiza personagem alternativa. Reconexão e troca de modelo redescobrem capabilities e resincronizam a emoção atual.

Lip sync usa amplitude real e injeta apenas boca. Mouse tracking injeta apenas olhos/cabeça encontrados no modelo, com smoothing e modos persistentes. Barge-in zera a boca sem apagar a emoção.

Diagnóstico:

- `GET /api/emotional-presence/status`
- `GET /api/live2d/settings`
- `POST /api/emotional-presence/test/{emotion}`
