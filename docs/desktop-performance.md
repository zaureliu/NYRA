# Desempenho do Desktop V3

Medição local em 2026-08-19, release Tauri 2/WebView2, overlay idle:

- executável: aproximadamente 10,3 MB;
- processo nativo: 30,3 MB de working set e 6,5 MB privados;
- CPU durante 3 segundos idle: 0%;
- bundle próprio do overlay: cerca de 20 kB, além do chunk React compartilhado.

A medição V3 incluindo a árvore WebView2, após 15 s de estabilização, registrou 1,849% CPU total do host, 366,6 MB working set e 201,8 MB private em idle. Durante turno falado: 2,232% CPU médio, 397,6 MB working set e 226,2 MB private. O processo nativo isolado permaneceu perto de 25 MB; WebView2 responde pela maior parte da memória.

A animação idle usa CSS lento. `requestAnimationFrame` só é usado durante áudio ou medição ativa do microfone.
