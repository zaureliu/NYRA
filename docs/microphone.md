# Microfone e VAD

A captura web solicita, quando suportado, `echoCancellation`, `noiseSuppression`, `autoGainControl`, mono e 48 kHz. O medidor exibe RMS, pico, clipping e fala detectada. Opus é decodificado no backend para mono/16 kHz antes do faster-whisper.

O backend usa Silero VAD v6 ONNX já incluído no faster-whisper, sem PyTorch ou nuvem. Padrões: threshold 0,5; fala mínima 250 ms; silêncio mínimo 650 ms; speech pad/pre-roll 250 ms; post-roll de captura 350 ms. Há ganho conservador e rejeição de silêncio.

Em **Configurações > Abrir Voice 2.0 > Microphone Test**, segure o botão, fale e solte. O painel permite ouvir a captura e mostra transcrição, duração e decisão do VAD. Entrada e saída ficam persistidas no navegador. O áudio temporário do backend é removido no `finally`.
