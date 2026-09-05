# Autorização da API VTube Studio

1. Abra VTube Studio pela Steam.
2. Habilite `Allow Plugin API access`.
3. Na KAZUMI, habilite Live2D e clique `AUTHORIZE`.
4. Aprove manualmente o popup `KAZUMI Avatar Bridge`.

O token é salvo em `data/secrets/vtube_studio_token.json`, diretório ignorado pelo Git. Ele nunca vai ao frontend, logs ou LLM. Revogar no VTS devolve o provider a `AUTH_REQUIRED`.
