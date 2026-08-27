# Pipeline de assets V3

1. Gerar arte original sem referência protegida.
2. Exigir fundo genuinamente transparente e uma única personagem.
3. Rejeitar checkerboard embutido, RGB sem alpha e halo retangular.
4. Validar dimensões, PNG RGBA e alpha 0 nos cantos.
5. Copiar a variante aprovada para nyra_v3/desktop ou portrait.
6. Atualizar manifest somente quando caminho, âncora ou renderer mudar.
7. Executar test_avatar_v3.py, Vitest e build.
8. Capturar sobre branco e preto.

A arte ativa veio da ferramenta imagegen integrada. O primeiro Portrait RGB foi rejeitado; a extração de fundo corrigida passou em Format32bppArgb e corner alpha 0.
