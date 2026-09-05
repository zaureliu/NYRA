# Sentinel Bridge Security

Propriedades da versão 1:

- read-only do ponto de vista da KAZUMI;
- Bridge e Watch OFF por padrão;
- Bearer token dedicado, forte e comparado em tempo constante;
- token fora do Git, frontend, URL, runtime settings e logs;
- fingerprint público mínimo; status, replay e stream autenticados;
- schema estrito, payload máximo de 32 KiB e metadata por allowlist;
- nenhum payload bruto, header, cookie, sessão, password, private key ou token exportado;
- bind e firewall permanecem decisões explícitas do operador;
- nenhum Cloudflare Tunnel ou exposição pública automática;
- nenhuma execução de shell, scan remoto, alteração de alerta ou comando Sentinel.

O prefixo HTTP passa pelo hardening geral até o autenticador dedicado da bridge. CORS não é usado como autenticação. Socket.IO valida `auth.token` no namespace exclusivo. Falha de autenticação produz `AUTH_FAILED` e espera o intervalo configurado, sem retry agressivo.

Rotação: gere outro token no Sentinel, atualize os dois `.env`/secret store e reinicie o Sentinel. A KAZUMI perde autenticação de forma visível e não recebe detalhes protegidos até o token correto ser salvo.

Logs registram estado, tipo de erro e contadores; nunca texto completo do evento nem segredo. MAC address não entra no schema público padrão. IP privado só permanece quando faz parte da allowlist de metadata técnica.
