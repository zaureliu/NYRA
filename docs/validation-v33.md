# Validação V3.3 — 2026-08-19

## Automação

- Backend: `37 passed`.
- Frontend: `7 passed` em 5 arquivos.
- TypeScript + Vite: aprovado.
- Tauri release + NSIS: aprovado; `nyra-desktop.exe` recompilado.
- `cargo fmt --check`: aprovado.
- `git diff --check`: aprovado (somente avisos de conversão LF/CRLF do Git para Windows).

## Network Watch real

Execução read-only de 65 segundos no host:

- interface: `Ethernet`;
- gateway: `192.168.1.1`, respondendo em aproximadamente 1 ms;
- Internet: disponível;
- latência média: 57,49 ms (mínima 42,38; máxima 105,55);
- perda: 0%;
- jitter da janela: 25,09 ms;
- DNS: disponível;
- 124 snapshots de interface;
- processo isolado de benchmark: 1,53% de CPU e 49,46 MB RSS.

O controle runtime foi ligado por 12 segundos e desligado novamente sem reiniciar o backend. A preferência final permaneceu OFF, pronta para ativação explícita no dashboard.

Com backend, Vite e Desktop Presence abertos e os recursos opt-in desligados, a amostra de 5 segundos mediu aproximadamente 0,11% da CPU total e 374,1 MB de working set somado (backend 241,3 MB incluindo launcher, Vite 107,9 MB e Desktop 24,9 MB).

Depois da ativação explícita pelo operador, `MIC ON` obteve lease exclusivo e Network Watch permaneceu `online`. A amostra de 5 segundos com ambos ativos mediu cerca de 0,16% da CPU total e 500,8 MB somados; o aumento de RAM veio principalmente do faster-whisper carregado no processo filho do backend (349,6 MB).

## Alertas proativos

Foram injetados `high_latency` e `network_recovered` pelo endpoint de debug. Ambos passaram pelo Event Bus, foram persistidos, exibidos no Desktop Presence e geraram WAV Edge TTS. Tempos observados do evento ao arquivo de áudio ficaram próximos de 2 segundos. O Desktop Presence permaneceu respondendo.

## Always Listening

Wake word, aliases locais, extração do comando, hands-free, encerramento natural, lease exclusivo, mute e self-voice guard foram validados por testes. O encoder do ring buffer gerou WAV PCM mono válido em teste frontend. A captura física permanece desligada por padrão e deve ser autorizada pelo operador em Settings; por essa razão o benchmark não abriu o microfone automaticamente.
