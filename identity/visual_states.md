# Estados visuais — apresentação VTS

A emoção canônica vem do Persona Runtime. O adapter VTS não recalcula emoção e só usa capabilities encontradas no modelo atual.

| Estado | Apresentação quando disponível |
|---|---|
| neutral/friendly | neutro ou binding configurado |
| focused/serious/warning | hotkey, expression ou parâmetro compatível |
| happy/relieved/positive | hotkey ou expression compatível |
| concerned/empathetic/apologetic | hotkey, expression ou parâmetro custom compatível |
| amused/curious/surprised | hotkey, expression ou parâmetro custom compatível |

Listening, thinking e speaking são estados operacionais. Speaking combina expression emocional com lip sync por amplitude. Mouse tracking controla apenas olhos/cabeça e permanece independente. Capability ausente não é inventada; VTS offline não aciona personagem alternativa.
