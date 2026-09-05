# KAZUMI Voice 2.0

O backend mantém o contrato `TTSProvider.synthesize(text, state, options)`. `KokoroTTSProvider` é o provider estável e `ChatterboxTTSProvider` executa o Chatterbox Multilingual 0.1.7 em `.venv-chatterbox`, isolado do NumPy/ONNX do MVP.

## Perfil ativo

- provider: `kokoro`; voz: `pf_dora`; idioma: pt-BR; perfil `KAZUMI_VOICE`.
- speaking rate: 0,88; pausas de sentença/parágrafo: 240/460 ms.
- Chatterbox: temperature 0,8; exaggeration 0,5; cfg weight 0,45; seed 42. Só são enviados parâmetros suportados.
- Não há alteração artificial de pitch nem referência de pessoa real. `data/voices/kazumi_reference.wav` é opcional e ignorado pelo Git.

`speech/prosody.py` separa `display_text` de `speech_text`, remove Markdown impróprio para fala, converte percentuais/durações, divide blocos e aplica `identity/pronunciation_ptbr.json`. O texto visual não é modificado.

Abra **Configurações > Visual + Voice Lab** para escolher provider/voz, exibir somente parâmetros suportados, sintetizar, salvar o perfil e ouvir/selecionar A/B.

O pacote Chatterbox e PyTorch CPU foram instalados no ambiente separado; o probe confirmou `torch 2.6.0+cpu`, sem CUDA. A RX 7600 não fornece CUDA. Com os pesos em cache, a frase casual levou 56,15 s na CPU e produziu WAV válido; por isso Chatterbox fica disponível para comparação, mas Kokoro permanece padrão responsivo.
