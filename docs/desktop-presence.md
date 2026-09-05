# Desktop Presence

Desktop Presence é uma janela Tauri transparente e VTS-only. O modelo atualmente carregado no VTube Studio chega por Spout2; o projeto não contém personagem, renderer ou modelo Live2D embutidos. Sem frames válidos, a camada de personagem fica vazia.

A janela preserva alpha, always-on-top, click-through, drag, escala, posição e menu existentes. O X apenas oculta para o tray. **Encerrar NYRA** para o tracker de mouse, conexão VTS, receiver Spout2, Presence, backend owned e desktop, sem encerrar VTube Studio, Ollama ou Voicemeeter.

## Mouse tracking

Uma thread nativa consulta `GetCursorPos` a aproximadamente 30 Hz. `GetSystemMetrics` fornece os bounds físicos do desktop virtual, incluindo origem negativa e múltiplos monitores. X é normalizado para `-1..1`; Y é invertido para cima positivo.

O backend aplica deadzone central de 5,5%, clamp, smoothing exponencial independente e limite de velocidade. Olhos respondem mais rápido; cabeça é mais suave e tem influência levemente reduzida durante TTS. Modos persistidos:

- `OFF`: zera uma vez os parâmetros de mouse e para de injetá-los;
- `EYES`: controla apenas olhos e zera a influência anterior de cabeça;
- `HEAD_EYES`: olhos rápidos e cabeça atrasada, padrão.

Só são enviados parâmetros realmente descobertos. Mouse, lip sync e emotion mapping usam conjuntos separados, evitando sobrescrever boca ou expression.

## Lifecycle

O backend owned é encerrado por token efêmero local e prazo limitado. O Job Object cobre crash do desktop. Processos externos nunca são alvos do shutdown. O tray e todas as janelas owned são removidos antes da saída final.
