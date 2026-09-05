# Voice Hunter — relatório técnico de 2026-08-19

Pesquisa real executada em fontes primárias. Doze opções entraram no catálogo final; outras famílias foram eliminadas antes do download. As notas de naturalidade e personagem são triagem, não substituem audição humana.

## Catálogo classificado

| Candidate | Source | Type | Language | Gender | License | Allowed Use | Reference Allowed | Size | Provider | Naturalness estimate | Integration difficulty | Status |
|---|---|---|---|---|---|---|---:|---:|---|---:|---|---|
| OmniVoice BR-PT Calm Design | edwixx + k2-fsa | Synthetic | BR-PT/pt-BR experimental | female designed | Apache-2.0 | local, derivative, redistribution | yes | 2.45 GB checkpoint | OmniVoice → Chatterbox | 8.2 | HIGH | SAFE_FOR_KAZUMI_REFERENCE |
| Kokoro Dora | hexgrad/Kokoro-82M | Synthetic | pt-BR | female packaged voice | Apache-2.0 | direct local TTS | no | ~326 MB existing | Kokoro ONNX | 7.3 | LOW | SAFE_FOR_DIRECT_TTS |
| Chatterbox default | ResembleAI/chatterbox | Synthetic | multilingual pt | synthetic female target | MIT | direct local TTS | no | existing install | Chatterbox | 7.0 | MEDIUM | SAFE_FOR_DIRECT_TTS |
| Edge Thalita | Microsoft | TTS Provider | pt-BR | female | service terms | personal audition through provider | no | online | Edge TTS | 8.6 | LOW | AUDITION_ONLY |
| Edge Francisca | Microsoft | TTS Provider | pt-BR | female | service terms | personal audition through provider | no | online | Edge TTS | 8.3 | LOW | AUDITION_ONLY |
| Azure Luana MAI | Microsoft Azure | TTS Provider | pt-BR | female | Product Terms/paid tier | direct paid TTS | no | online | Azure Speech | 9.1 | MEDIUM | SAFE_FOR_DIRECT_TTS / PAID_OPTION |
| Azure Manuela | Microsoft Azure | TTS Provider | pt-BR | female | Product Terms/paid tier | direct paid TTS | no | online | Azure Speech | 8.6 | MEDIUM | SAFE_FOR_DIRECT_TTS / PAID_OPTION |
| Google Neural2-A | Google Cloud | TTS Provider | pt-BR | female | Cloud Service Terms | direct paid TTS | no | online | Google Cloud TTS | 8.4 | MEDIUM | SAFE_FOR_DIRECT_TTS / PAID_OPTION |
| F5-TTS pt-BR | firstpixel/fuuuzzy | Model | pt-BR | reference-dependent | CC-BY-NC-4.0 | non-commercial audition | no | 1.35 GB | F5-TTS | 8.4 | HIGH | AUDITION_ONLY |
| Common Voice individual | Mozilla | Dataset | pt-BR | metadata-dependent | CC0 + dataset terms | corpus research/audition | no | not downloaded | dataset | 8.0 | HIGH | AUDITION_ONLY |
| Piper pt-BR current | rhasspy/piper-voices | Model | pt-BR | not documented as suitable female | per-voice | unresolved for target | no | not downloaded | Piper | 6.5 | LOW | REJECTED |
| Qwen3-TTS VoiceDesign 1.7B | QwenLM | Synthetic | Portuguese, pt-BR unconfirmed | female designed | Apache-2.0 | local synthetic voice design | yes | not downloaded | Qwen3-TTS | 8.5 | HIGH | SAFE_FOR_KAZUMI_REFERENCE |

Contagem: 7 elegíveis por licença para referência ou TTS direto, 4 apenas para audição e 1 rejeitada. Três das sete elegíveis são opções pagas não ativadas; Qwen3 está elegível juridicamente, mas não foi instalado nem tecnicamente validado neste host.

## Pré-filtro sem download

Também foram verificados OpenSLR CML-TTS/MLS (9,7/9,3 GB, pessoas reais), Meta MMS português (CC-BY-NC e sem pt-BR/feminino garantido), CosyVoice 3 (sem português), Spark-TTS (chinês/inglês), MeloTTS (sem suporte pt-BR documentado), Orpheus (sem modelo oficial pt-BR), além de categorias Fish Speech e StyleTTS2 sem uma combinação oficial atual de voz feminina pt-BR, licença e integração segura. Nenhum executável de terceiro foi usado.

## Downloads e proveniência

Foi baixado somente o modelo promissor OmniVoice, do model card oficial, dentro de venv isolada. A voz foi criada por voice design (`female, young adult, low pitch, portuguese accent`), seed 3404, sem referência humana.

- amostra normalizada: 5,040 s, mono PCM16/24 kHz;
- SHA-256: `aaf981f4a97abf453abc97fea67ca79021691bb54172346ff3da37b62ff06ddb`;
- RMS: -24,23 dBFS; pico 0,531; clipping 0; silêncio 45,95%; SNR aproximado 24,9 dB;
- faster-whisper: idioma `pt`, confiança 1,0; transcrição “Oi, eu sou a naíra. Acho que agora estamos chegando mais perto da minha voz.”

Cinco samples estão prontos nos cards: OmniVoice, Kokoro, Chatterbox default, Edge Thalita e Edge Francisca. Apenas o sample OmniVoice é uma referência clonável; os Edge são previews gerados para audição.

Uso em disco medido: 2,295 GiB do checkpoint OmniVoice + 0,750 GiB do tokenizer + 3,796 GiB da venv isolada + 0,001 GiB de candidatos + 0,015 GiB de benchmarks = **6,857 GiB**, abaixo do teto de 8 GiB. Instalações preexistentes de Kokoro/Chatterbox não foram contabilizadas como novos downloads.

## Benchmark real

Todos os providers receberam as mesmas oito entradas (sete frases solicitadas e uma long-form), já tratadas pelo Pronunciation Engine V3.2. O modelo/worker permaneceu residente. `warm` é a mediana das gerações após o health/cold start; `total` soma as oito sínteses.

| Candidate / execução | Cold ms | Warm median ms | Total 8 frases ms | Long-form áudio | Long-form synth | Long RTF |
|---|---:|---:|---:|---:|---:|---:|
| Edge Francisca | 0,0* | 2.228,2 | 20.664,6 | 31,822 s | 5.635,6 ms | 0,177 |
| Edge Thalita | 1.658,9 | 2.574,4 | 26.900,3 | 32,406 s | 8.776,8 ms | 0,271 |
| Kokoro Dora | 4.450,9 | 5.901,5 | 75.931,7 | 30,308 s | 34.768,8 ms | 1,147 |
| Chatterbox + OmniVoice ref | 7.597,8 | 26.321,8 | 367.276,6 | 30,147 s | 160.614,6 ms | 5,328 |
| Chatterbox default | 9.955,3 | 29.745,6 | 363.041,3 | 28,740 s | 154.026,4 ms | 5,359 |

\* Francisca reutilizou o provider Edge já aquecido; portanto zero não é uma medição independente de cold start. Time-to-first-audio não é exposto pelos providers atuais e fica explicitamente `null`.

O Whisper confirmou português com confiança 1,0 nas amostras. Edge teve a melhor inteligibilidade técnica desta rodada; Kokoro ficou utilizável, mas ainda confundiu Proxmox; Chatterbox confundiu vários termos técnicos. A referência OmniVoice melhorou a frase casual do Chatterbox, mas não resolveu a fala técnica e não melhorou a latência.

## Recomendações para audição

### BEST OVERALL

Não há escolha definitiva sem audição humana. **Kokoro Dora** é a melhor candidata técnica elegível e operacional no conjunto atual: local, licença permissiva, estável e muito mais rápida que Chatterbox.

### BEST LOCAL

**Kokoro Dora**. OmniVoice é a alternativa local mais interessante para identidade própria, porém funciona aqui como gerador de referência e não como TTS conversacional de baixa latência.

### BEST LOW LATENCY

**Edge Francisca** no benchmark, mas somente `AUDITION_ONLY`. Entre opções selecionáveis e já integradas, **Kokoro Dora**.

### BEST NATURALNESS

As notas de catálogo apontam **Azure Luana MAI** como hipótese mais promissora, mas ela é paga e não foi testada. Entre samples prontos, a decisão precisa ser do usuário após A/B; nenhuma métrica automática sustenta uma vencedora.

### BEST TECHNICAL SPEECH

**Edge Francisca** nesta rodada de STT, bloqueada para identidade permanente. Entre as elegíveis locais, **Kokoro Dora**, ainda com necessidade de aliases adicionais para Proxmox/OpenWrt.

### BEST CHATTERBOX REFERENCE

**OmniVoice BR-PT Calm Design** é a melhor referência segura testada — e a única desta rodada sem voz humana de terceiro. É uma recomendação de segurança/proveniência, não uma afirmação de superioridade sonora.

## Top 5 para o usuário ouvir

1. Kokoro Dora — local e selecionável.
2. OmniVoice Calm Design + Chatterbox — identidade sintética própria e selecionável, mas lenta.
3. Edge Francisca — melhor latência/técnica, somente audição.
4. Edge Thalita — baseline online atual, somente audição.
5. Chatterbox default — baseline local direto; baixa inteligibilidade nesta execução.

O perfil `KAZUMI_VOICE` não foi alterado e `data/voices/kazumi_reference.wav` não foi criado. A escolha continua exclusivamente na interface.
