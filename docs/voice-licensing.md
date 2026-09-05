# Política de licenciamento de voz

Licença do código/modelo, licença do dataset, direitos sobre a gravação e autorização para derivar uma identidade vocal são perguntas diferentes. O Voice Hunter nunca converte “dataset aberto” em “clonagem permitida”.

## Regra de classificação

| Status | Uso no Voice Hunter | Pode virar oficial? |
|---|---|---:|
| `SAFE_FOR_KAZUMI_REFERENCE` | sample sintético/licenciado pode ser usado como referência | sim, após clique do usuário |
| `SAFE_FOR_DIRECT_TTS` | geração pelo provider nos termos aplicáveis | sim, somente como provider direto |
| `AUDITION_ONLY` | escuta e comparação | não |
| `REJECTED` | registro documental | não |

Para `SAFE_FOR_KAZUMI_REFERENCE`, os termos precisam permitir o uso derivativo necessário, e a procedência não pode depender de uma pessoa identificável sem consentimento específico. Uma voz sintética licenciada tem prioridade quando a qualidade é comparável.

## Decisões desta busca

- OmniVoice/Qwen3 VoiceDesign: código e pesos sob Apache-2.0 e geração por descrição, sem gravação humana fornecida. A amostra OmniVoice gerada localmente é elegível como referência; o Qwen3 não foi baixado nem validado para pt-BR nativo.
- Kokoro e Chatterbox oficial: uso direto permitido pelas licenças Apache-2.0/MIT. A saída não é promovida automaticamente a referência de clonagem.
- Edge Read Aloud: permitido aqui apenas para audição pessoal. Os termos do endpoint não foram tratados como autorização para redistribuir ou clonar a voz.
- Azure/Google pagos: catalogados como TTS direto. Exigem conta, credencial, cobrança e revisão dos termos vigentes; nada foi ativado.
- F5-TTS pt-BR: CC-BY-NC-4.0 e referência externa obrigatória; identidade/dataset insuficientemente claros para uma voz permanente.
- Common Voice/OpenSLR: licenças de corpus não substituem consentimento para transformar um falante real em identidade derivada. Nenhum áudio individual foi baixado.
- Piper: cada voz tem licença própria; a fonte primária atual não documenta uma opção feminina adequada. Gênero não foi inferido pelos nomes.

## Fontes primárias

- [OmniVoice BR-PT model card](https://huggingface.co/edwixx/omnivoice-brpt-v15) e [repositório oficial](https://github.com/k2-fsa/OmniVoice)
- [Qwen3-TTS oficial](https://github.com/QwenLM/Qwen3-TTS)
- [Chatterbox oficial](https://github.com/resemble-ai/chatterbox)
- [Kokoro-82M model card](https://huggingface.co/hexgrad/Kokoro-82M)
- [Microsoft: idiomas e vozes](https://learn.microsoft.com/azure/ai-services/speech-service/language-support)
- [Google Cloud: lista de vozes](https://cloud.google.com/text-to-speech/docs/voices)
- [Mozilla Common Voice Terms](https://commonvoice.mozilla.org/terms)
- [OpenSLR 146](https://www.openslr.org/146/) e [OpenSLR 94](https://www.openslr.org/94/)
- [Piper pt-BR](https://huggingface.co/rhasspy/piper-voices/tree/main/pt/pt_BR)

Links e notas são armazenados também no `source.json`/`license.txt` de cada amostra. Uma descrição de mecanismo de busca nunca é aceita como prova de licença.
