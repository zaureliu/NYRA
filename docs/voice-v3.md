# Voz V3 / custom Chatterbox pt-BR

O perfil persistido continua `NYRA_VOICE` em Kokoro `pf_dora`, o fallback feminino pt-BR rápido deste host. O Voice Lab agora oferece A/B/C: Kokoro, Chatterbox Multilingual V3 e Chatterbox V3 com `data/voices/nyra_reference.wav` autorizado. A referência é importada como WAV, validada, convertida para 24 kHz e normalizada levemente; o arquivo é protegido pelo `.gitignore`.

O provider genérico usa worker residente: cold start medido em 39,17 s e warm generation em 20,36 s para a frase curta, enquanto as gerações seguintes reutilizam a instância. Falha, timeout ou ausência do provider retorna a Kokoro. O checkpoint oficial `ResembleAI/Chatterbox-Multilingual-pt-br` está catalogado, mas o pacote local `chatterbox-tts==0.1.7` não possui loader para os assets single-language; por isso aparece indisponível até um loader compatível/assets serem instalados. Não houve download automático.

Perfil oficial: NYRA_VOICE, Kokoro pf_dora, speaking rate 0.88, sentence pause 240 ms e paragraph pause 460 ms. Não há pitch shift ou referência de pessoa real.

O Voice Lab consulta supported_parameters. Kokoro mostra voice/rate/pausas; Chatterbox mostra temperature/exaggeration/CFG/seed/pausa. O A/B guarda A e B separadamente, permite repetir e selecionar antes de salvar.

## Benchmark real

| Frase Kokoro | Síntese | Duração |
|---|---:|---:|
| casual | 4,42 s | 4,10 s |
| normal | 3,89 s | 3,65 s |
| técnica | 8,83 s | 8,41 s |
| curiosa | 4,71 s | 4,50 s |
| humor seco | 4,04 s | 3,39 s |
| alerta | 6,47 s | 5,70 s |

Chatterbox casual: 36,92 s de síntese e 3,56 s de áudio; 44,75 s ponta a ponta no script. Ambos os WAVs casuais foram retranscritos integralmente pelo faster-whisper, language probability 1.0, sem clipping. A seleção oficial considera amostras reais, inventário, idioma, gênero declarado e latência. Avaliação auditiva humana continua disponível no Voice Lab.

display_text nunca passa pelo dicionário fonético. speech_text limpa Markdown, divide blocos, normaliza números/percentuais e aplica identity/pronunciation_ptbr.json.
