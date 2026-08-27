# Privacidade da percepção

O Perception Engine é local-first, visível e desligável em runtime. Em `Settings > Privacy > Perception` existem controles separados para aplicação ativa, título, mouse, idle, métricas e captura de tela.

Garantias V4:

- nenhuma tecla ou texto digitado é capturado;
- clipboard, mensagens e campos não são lidos;
- nenhuma trajetória detalhada do mouse é armazenada;
- título completo fica desativado por padrão;
- screen capture permanece OFF e sua ativação é rejeitada pela validação;
- snapshots rotineiros não são memorizados;
- active app, mouse, Sentinel e métricas não são enviados a serviços externos desnecessariamente;
- Edge TTS recebe somente `speech_text` quando selecionado.

Desligar `Perception Engine` cancela o worker de sensores. O indicador de microfone e as opções do Always Listening continuam independentes e explícitos.
