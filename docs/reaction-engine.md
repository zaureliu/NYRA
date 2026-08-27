# Reaction Engine

O `ReactionEngine` transforma eventos, percepção, atenção e cooldown em mudanças de avatar e, excepcionalmente, fala. Alterar de aplicação normalmente produz no máximo uma microreação visual; alertas críticos são determinísticos.

Prioridades iniciais:

| Fonte | Prioridade |
|---|---:|
| Usuário falando | 100 |
| Sentinel/Network crítico | 90 |
| Warning | 70 |
| Aplicação ativa | 40 |
| Idle | 20 |

`AttentionEngine` aplica decay e volta a `neutral`. Cooldowns são centralizados, e `ProactiveEngine` limita falas de baixa prioridade por hora; críticos não usam probabilidade nem esse orçamento. Usuário falando, áudio ativo e contexto de call bloqueiam comentários casuais quando o sinal local está disponível.

Saídas suportadas incluem expressão, olhos, inclinação da cabeça, corpo, Neural Link, animação, notificação e fala. A política padrão privilegia olhar, blink e head tilt: silêncio é uma reação válida.
