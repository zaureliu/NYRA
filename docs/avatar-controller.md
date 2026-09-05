# Avatar Controller

`AvatarController` conserva o estado compartilhado de boca, emoção e estados operacionais. Ele não renderiza personagem. O único consumidor visual é o adapter VTube Studio, que envia exclusivamente IDs encontrados no modelo atualmente carregado.

Lip sync deriva da amplitude real. Mouse tracking controla somente olhos e cabeça, portanto pode coexistir com boca, hotkeys e expressions. Listening, thinking e speaking são estados operacionais e não substituem a emoção canônica.

A integração usa WebSocket local autenticado e Spout2. Não instala VTube Studio, não copia modelo Live2D para o projeto e não contém fallback visual interno.
