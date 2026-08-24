# Estados visuais V3

| Estado | Rosto/olhos | Neural Link | Movimento/voz |
|---|---|---|---|
| idle/neutral | aberto, calmo | quase apagado | respiração lenta |
| listening | brilho ciano discreto | aceso | postura atenta |
| thinking | olhos half/topologia | pulso lento | microinclinação |
| speaking | contato visual + lip sync | atividade curta | boca por amplitude |
| offline | brilho reduzido | cinza | imóvel |
| happy | sorriso mínimo | normal | voz levemente mais leve |
| curious | cabeça/olhar inclinado | normal | entonação sutil |
| focused | olhar firme | pulso contido | dicção precisa |
| concerned | sério, brilho menor | indicador reduzido | voz mais lenta |
| amused | sorriso lateral | normal | ironia baixa |
| tired | olhos half | baixo | energia menor |
| surprised | boca aberta breve | pulso curto | reação sem exagero |

O EventBus é a fonte operacional: USER_SPEECH_RECEIVED → listening, LLM_PROCESSING → thinking, TTS_STARTED → speaking, TTS_FINISHED → idle.
