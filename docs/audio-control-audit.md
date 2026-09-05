# Auditoria de controles de áudio (baseline V1)

Data da auditoria: 2026-08-22. O critério `WORKING` estrito exige o caminho completo
UI → API → persistência → runtime e um efeito observável/testável. Ter um handler React
ou gravar `localStorage` não foi considerado prova de funcionamento.

| Superfície antiga | Controles estáticos | UI | Backend recebe | Runtime usa | Resultado da auditoria |
|---|---:|---|---|---|---|
| Settings / devices | 3 | sim | parcial | sim, por caminhos separados | PARTIAL / DUPLICATE |
| Realtime Settings | 17 | sim | sim | parcial | PARTIAL / UNNECESSARY |
| Listening Settings | 12 | sim | sim | parcial; `vad_threshold` não reconfigurava o STT carregado | PARTIAL / DUPLICATE |
| Microphone Test | 2 | sim | sim | sim | WORKING, sem teste de persistência |
| Voice Lab | 32 | sim | sim | isolado do runtime normal | EXPERIMENTAL / UNNECESSARY |
| Voice Processor Lab | 11 | sim | sim | DSP real, mas fora do objetivo e com custo no caminho normal | EXPERIMENTAL |
| Voice Hunter | 14 | sim | sim | pesquisa, não seleção do runtime normal | EXPERIMENTAL |
| **Total** | **91** | | | | **0 comprovados end-to-end pelo critério estrito** |

Problemas concretos encontrados:

- microfone e speaker eram persistidos no frontend, enquanto o backend mantinha outros defaults;
- `Settings.from_sources` mascarava valores de `.env` presentes também no YAML;
- barge-in existia em dois modelos de configuração;
- a UI suspendia a captura enquanto a KAZUMI falava, tornando interrupção natural impossível;
- a factory consultava Chatterbox antes de respeitar uma seleção explícita de Kokoro;
- Voice Lab, Voice Hunter e Voice Processor competiam visualmente com as escolhas normais;
- o launcher e o backend executavam warm-ups diferentes;
- toda conversa, inclusive casual, recebia o Agent Loop e schemas de tools;
- o playback real podia ocorrer depois do fechamento da telemetria e não atualizava TTFA.

## Superfície V2

A tela normal ficou com sete preferências funcionais: microfone, speaker, velocidade,
volume, modo, Always Listening e interrupção. A voz primária aparece como informação,
não como seletor falso, porque nesta versão há uma única voz suportada no runtime:
Kokoro `pf_dora`. Há duas ações reais de teste (microfone/STT e voz/TTS) e playback
da gravação do teste de microfone.

Assim, 80 dos 91 elementos interativos antigos saíram da experiência principal. Os
sete ajustes finais foram consolidados no schema `AudioSettingsUpdate` e todos têm
consumidor no runtime. Voice Hunter continua disponível somente como serviço de pesquisa
e scripts técnicos; não participa da escolha normal de voz.

## Evolução: provider layer opcional

A conclusão acima permanece como registro da baseline V2 para o engine local.
A camada posterior de providers adiciona um seletor lógico funcional
Local/OpenAI/ElevenLabs, toggle online explícito, modelo/voz, Credential Broker e
teste manual. Ela não substitui a escolha local histórica do Voice Lab: com o
toggle desligado, o runtime continua integralmente local. Consulte
[online-voice-providers.md](online-voice-providers.md).
