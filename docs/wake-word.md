# Wake Word

`WakeWordProvider` desacopla a política de ativação do mecanismo. A V3.3 usa `TranscriptWakeWordProvider`: o áudio é primeiro limitado pelo VAD e transcrito localmente pelo faster-whisper; a detecção de “Nyra” acontece sobre essa transcrição local.

O provider reconhece `Nyra` e duas grafias comuns do Whisper em pt-BR (`Nira`, `Naira`), somente no começo da fala. Menções no meio de uma conversa não ativam a NYRA. Fala sem wake word não chega ao LLM fora da janela hands-free.

Esse desenho permite substituir o provider por um modelo acústico local no futuro sem alterar API, sessão ou UI. Nenhum serviço cloud recebe áudio do usuário.
