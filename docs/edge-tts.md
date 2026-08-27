# Edge TTS V3.1

Edge TTS é um provider **online** opcional. Ele recebe somente o `speech_text` já preparado pela prosódia; memória, prompts, ferramentas, logs e credenciais não são enviados.

O inventário é consultado dinamicamente pela biblioteca `edge-tts`, cacheado por uma hora e filtrado por `pt-BR` + `Female` no Voice Lab. `Atualizar vozes Edge` força uma nova consulta. Se a rede falhar, o cache da sessão é preservado e a síntese cai para Kokoro.

Controles aceitos pelo Edge são `edge_rate` (`-25%` a `+15%` na UI), `edge_pitch` (`-20Hz` a `+20Hz`) e `edge_volume`. O formato usa as strings oficiais do provider, incluindo `+0Hz` para pitch neutro.

O áudio MP3 recebido é convertido uma única vez para WAV PCM usando PyAV, preservando a análise de duração/lip sync já existente. Streaming não foi ativado: a versão atual gera o arquivo completo para manter ordem, playback e lip sync estáveis.

A voz oficial não muda automaticamente. Use `Definir como voz oficial da NYRA` depois de ouvir e comparar as candidatas.
