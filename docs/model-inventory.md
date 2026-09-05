# Inventário de modelos

Inventário validado em 2026-08-19. Arquivos binários não são versionados.

| Finalidade | Modelo | Tamanho | SHA-256 |
|---|---|---:|---|
| LLM | Ollama `qwen3:8b` Q4_K_M | 5,2 GB (relatado pelo Ollama) | gerenciado pelo Ollama |
| LLM candidate | Ollama `qwen3.5:9b` Q4_K_M | 6.594.474.711 bytes / 6,6 GB | `6488c96fa5fa...` gerenciado pelo Ollama |
| STT | Systran faster-whisper-tiny | cache do Hugging Face | gerenciado pelo Hugging Face Hub |
| TTS | `kokoro-v1.0.int8.onnx` | 92.361.271 bytes | `6E742170D309016E5891A994E1CE1559C702A2CCD0075E67EF7157974F6406CB` |
| Vozes TTS | `voices-v1.0.bin` (`pf_dora`) | 28.214.398 bytes | `BCA610B8308E8D99F32E6FE4197E7EC01679264EFED0CAC9140FE9C29F1FBF7D` |

O setup verifica os hashes dos assets Kokoro antes de reutilizá-los. O adaptador usa CPU e a voz feminina pt-BR `pf_dora`. O projeto kokoro-onnx declara licença MIT para o código e Apache-2.0 para o modelo: [repositório oficial](https://github.com/thewh1teagle/kokoro-onnx) e [release dos modelos](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0).

## Ollama V5

| Modelo | Família | Quantização | Contexto testado | Runtime GPU | Status |
|---|---|---|---:|---:|---|
| `qwen3:8b` | qwen3 | Q4_K_M | 8192 | 6,2 GB, 100% GPU | instalado, baseline e oficial |
| `qwen3.5:9b` | qwen35 multimodal | Q4_K_M | 8192 | 5,6 GB, 100% GPU | instalado, candidate benchmarked |

Instalação do 9B: 2026-08-19 via `ollama pull`; o 8B não foi removido. Embora a fonte declare até 256K para o 9B, a KAZUMI usa 8192 para latência/VRAM previsíveis.
