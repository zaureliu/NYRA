# TTS benchmark V3.1

Execução real no host local, com a frase de introdução Edge:

| Provider | Voz | Resultado | Tempo |
|---|---|---|---:|
| Edge TTS | `pt-BR-ThalitaMultilingualNeural` | WAV PCM, 317996 bytes | 1,382 s |
| Edge TTS inventory | 2 vozes pt-BR femininas | Thalita, Francisca | consulta real |
| Kokoro | `pf_dora` | benchmark V3 casual | 4,42 s |
| Chatterbox V3 | genérico | cold / warm | 39,17 s / 20,36 s |

`time_to_first_audio` específico de streaming não foi medido porque o provider atual entrega o arquivo completo; o tempo de síntese do Edge é usado como aproximação de time-to-first-audio. Streaming fica preparado como evolução futura, sem comprometer estabilidade agora.

A preferência técnica inicial de teste é `pt-BR-ThalitaMultilingualNeural` por aparecer primeiro no catálogo atual, mas nenhuma voz foi declarada oficialmente melhor sem audição humana e nenhuma alteração automática foi feita.
