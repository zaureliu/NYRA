# Validação do MVP

## Atualização Voice 2.0/Desktop — 2026-08-19

- Backend: **17 testes aprovados**; frontend: TypeScript/build multipágina e **1 teste Vitest aprovado** para backoff WebSocket.
- Desktop: `cargo check` e build release Tauri aprovados; executável Windows gerado.
- Cinco WAVs Kokoro foram gerados pelas frases fixas em 4,45–7,98 s, com 200–373 kB.
- Chatterbox: pacote 0.1.7, PyTorch 2.6 CPU, sem CUDA; amostra WAV válida, 56,15 s com cache quente. Kokoro permanece fallback ativo por latência.
- A release Tauri foi aberta no Windows; WebView2, avatar, transparência e posição no monitor direito foram confirmados em uma segunda captura. O fundo interno do SVG encontrado na primeira captura foi ocultado somente no renderer desktop.
- Consumo nativo idle: 30,3 MB de working set e 0% CPU em janela de 3 segundos.
- Pipeline VAD/Whisper revalidado com WAV pt-BR: 4,288 s de áudio, RMS 0,078651, pico 0,793762, sem clipping; Silero detectou 4.288 ms de fala e o Whisper transcreveu em 0,381 s. A captura física permanece um teste interativo no Microphone Test, pois o app não ativa o microfone silenciosamente.

Executada no ambiente descrito em `environment-report.md`, em 2026-08-19.

## Automatizada

- Backend: **9 testes aprovados** (`pytest`), cobrindo configuração/secrets, SQLite/FTS5, exclusão/importância, event bus, ferramentas/allowlist, API e pipeline com LLM mock.
- Frontend: TypeScript e bundle Vite aprovados; 25 módulos transformados, bundle JS gzip de aproximadamente 64,8 kB.
- npm audit: 0 vulnerabilidades reportadas na instalação.

## Operacional

- Backend e frontend iniciados por `scripts/start.ps1`.
- Frontend HTTP: 200.
- Health real: `status=online`, `llm=true`, `memory=true`, `stt=true`, `tts=true`.
- Providers: Ollama / `qwen3:8b`, faster-whisper, Kokoro ONNX / `pf_dora`.
- Ferramenta `get_local_system_stats`: READ_ONLY, executada com sucesso.
- WebSocket: handshake e evento `CONNECTED` recebidos.
- Áudio servido pela API: HTTP 200, 213.036 bytes no turno real.

## Primeira conversa real

Entrada: `Kazumi, você está online?`

Ollama respondeu com HTTP 200; pergunta e resposta foram persistidas como short-term. A resposta passou pelo Kokoro e gerou WAV. O modelo emitiu um emoji nesse primeiro turno; o system prompt foi ajustado depois para evitá-los por padrão.

## STT real

Um WAV pt-BR gerado pelo TTS foi enviado ao mesmo endpoint usado pelo microfone web. faster-whisper retornou texto, idioma `pt`, probabilidade 1,0 e duração de 3,73 s. O modelo `tiny` transcreveu “Kazumi” como “Nira”; modelos maiores podem melhorar nomes próprios ao custo de RAM/latência. Captura física depende de o operador conceder permissão de microfone ao navegador, por isso não pode ser acionada silenciosamente pelo setup.

## Correção relevante

O primeiro fallback SAPI importava corretamente, mas falhou em tempo de execução com acesso COM negado. O health anterior era superficial. SAPI foi isolado em subprocesso, o health passou a testar inferência real e Kokoro ONNX int8/`pf_dora` tornou-se o fallback funcional do host.
