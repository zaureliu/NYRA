# Hands-Free

Uma chamada válida com “Nyra” abre uma janela hands-free de 120 segundos. Cada interação aceita renova o prazo. As frases “pode parar de ouvir”, “encerra a conversa”, “até depois” e “pode ficar quieta agora” encerram somente essa sessão.

Autoproteção de voz:

```text
TTS_STARTED -> reconhecimento suspenso
playback iniciado -> speaking guard mantido
playback terminou -> guard de 400 ms -> captura plena
```

Dashboard e Desktop Presence notificam o backend no início/fim do playback. O barge-in está preparado por `SpeechQueue.clear()` e pelo evento `SPEECH_CANCELLED`, porém permanece experimental e desligado: sem cancelamento acústico confiável, permitir interrupção durante os alto-falantes aumentaria falso positivo da própria voz.
