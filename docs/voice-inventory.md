# Inventário de voz

| Provider | Voz | pt-BR | Estado neste host |
|---|---|---:|---|
| Kokoro ONNX 0.6.1 | `pf_dora` | nativa | funcional e fallback; candidato Voice Hunter |
| Chatterbox Multilingual 0.1.7 | modelo padrão | multilíngue (`pt`) | funcional em CPU; 36,92 s de síntese/44,75 s ponta a ponta na frase casual V3 |
| Chatterbox | `kazumi_reference` | depende de WAV autorizado | arquitetura pronta; nenhum WAV fornecido |
| Chatterbox Multilingual V3 | `chatterbox_multilingual_v3` | `pt` | worker residente; referência opcional |
| Chatterbox Single Language Pack | `chatterbox_ptbr` | `pt-BR` | checkpoint oficial catalogado; loader/assets local ainda indisponível |
| Edge TTS | `pt-BR-ThalitaMultilingualNeural` | `pt-BR` feminina | online; inventário real atualizado nesta sessão |
| Edge TTS | `pt-BR-FranciscaNeural` | `pt-BR` feminina | online; inventário real atualizado nesta sessão |
| OmniVoice BR-PT v1.5 | voice design sintético | BR-PT experimental | venv isolada; sample seguro pronto; CPU RTF ~5,86 |
| Qwen3-TTS VoiceDesign 1.7B | design feminino | Portuguese; pt-BR não confirmado | catalogado, não baixado por hardware/orçamento |

Última atualização do inventário Edge: sessão V3.1. A fonte é dinâmica; as duas vozes acima estavam disponíveis no momento da consulta e podem mudar no serviço online.

O pacote Kokoro contém 54 identificadores, mas `pf_dora` é a única voz feminina com prefixo português. Vozes de outros idiomas não são apresentadas como pt-BR apenas por terem nomes femininos. O Voice Hunter gerou cinco conjuntos reais, com oito frases por provider/candidata, em `data/voice-benchmarks/voice-hunter/` (ignorado pelo Git). Consulte [relatório](voice-hunter-report.md), [uso](voice-hunter.md) e [licenciamento](voice-licensing.md).

O perfil oficial existente continua apontando para Edge Thalita; esta etapa não alterou `KAZUMI_VOICE` automaticamente.
