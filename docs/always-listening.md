# Always Listening

O Hands On/Always Listening da NYRA vem ativo em instalações novas, no modo `hands_free`. O dashboard ou o Desktop Presence adquire um lease exclusivo de captura; isso impede dois clientes de enviarem a mesma fala. Uma preferência explicitamente salva pelo usuário continua prevalecendo.

Pipeline:

```text
microfone -> ring buffer PCM em memória -> energy gate -> utterance WAV temporário
-> Silero VAD local -> faster-whisper local -> wake word/session manager -> LLM
```

O ring buffer mantém o pre-roll configurado (350 ms por padrão) e encerra a fala após 550 ms de silêncio. O arquivo temporário existe somente durante a transcrição e é removido em `finally`. Áudio contínuo não é escrito em disco.

Modos:

- `push_to_talk`: preserva o comportamento manual.
- `wake_word`: exige “Nyra” fora de uma sessão ativa.
- `hands_free`: aceita fala detectada enquanto o modo estiver ativo.

O indicador `MIC ON/OFF/LISTENING/PROCESSING` nunca é ocultado quando a captura contínua está autorizada. Mutar encerra as tracks do navegador, não apenas ignora a transcrição. `Ctrl+Shift+M` alterna mute pelo Desktop Presence.

No startup, a UI enumera entradas, preserva um dispositivo salvo quando ele ainda existe e usa o microfone padrão como fallback. `devicechange` trata conexão, remoção e reconexão. Permissão negada e ausência de microfone mantêm o chat por texto disponível e não entram em loop de captura; depois de uma mudança de permissão ou dispositivo, a preparação é tentada novamente.

Configuração persistente fica em `data/settings-v33.json`, ignorado pelo Git.
