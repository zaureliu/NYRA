# KAZUMI Voice Processor

O processador roda depois do TTS e antes da reprodução. A implementação local usa NumPy e SoundFile e preserva duração/sample rate. O preset natural aplica somente processamento conservador: high-pass leve, presença/EQ sutil, compressão suave, limiter e ganho final.

Perfis disponíveis: `natural`, `focused`, `concerned`, `amused` e `alert`. A `KAZUMI Digital Signature` é opcional e quase imperceptível; não há pitch/formant extremo, autotune, bitcrusher ou vocoder.

Em `Settings > Voice Lab > Voice Processor`, o usuário controla Enabled, preset, EQ, compression, presence, output gain e signature, e gera A/B do mesmo áudio base em `Original TTS` e `KAZUMI Processed`.

Validação real do sample usado:

| Variante | Duração | Sample rate | Peak | RMS | Clipping |
|---|---:|---:|---:|---:|---:|
| Raw | 5376 ms | 24 kHz mono | 0,4385 | 0,0715 | não |
| Processed | 5376 ms | 24 kHz mono | 0,4103 | 0,0656 | não |

O processamento pode ser desligado sem afetar os providers existentes.
