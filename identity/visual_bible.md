# Bíblia visual — NYRA Avatar V2 (ativa)

A fonte de verdade visual integrada é `frontend/src/assets/nyra-v2/master/nyra-avatar-master.png`. NYRA mantém rosto oval delicado, olhos grandes azul-turquesa, cabelo longo loiro-mel com franja dividida, acabamento anime premium, aparência adulta e presença calma/acolhedora. A roupa oficial é contemporary Japanese feminine casual: blusa creme de gola delicada com cardigan vinho/ameixa. Headphones over-ear graphite/dark navy, com acentos mínimos violeta/ciano, ficam fisicamente encaixados no cabelo e nas orelhas.

A arte oficial é chest-up, RGBA transparente e usa um único canvas `1086×1448`. Estados de eyes/mouth nunca regeneram rosto, cabelo, roupa ou headphones. Toda variação deriva da master interna e conserva os landmarks do `frontend/public/avatar/nyra_v2/avatar-manifest.json`.

Evitar: mudança de identidade, cabelo violeta, roupa técnica/cyberpunk, Neural Link no lugar de headphones, uniformes, quimono, cosplay, aparência infantil, sexualização, headset gamer, logos, neon excessivo e geração independente de expressões completas.

## Histórico legado — NYRA V3

NYRA é uma IA feminina de homelab com apresentação adulta jovem (22–26 anos apenas como aparência visual), postura calma, observadora e confiante. Ela não é humana e não usa design infantil ou sexualizado.

## Assinaturas

- rosto oval delicado, expressão padrão serena e ligeiramente séria;
- olhos teal/azul-petróleo com ciano discreto e topologia interna quase imperceptível;
- cabelo longo violeta escuro/ameixa fria, franja assimétrica, reflexos roxos discretos e mecha interna teal;
- duas peças auriculares finas de grafite: NYRA Neural Link;
- blusa técnica, jaqueta curta grafite assimétrica e calça técnica preta;
- símbolo de três nós conectados: memória, rede e inteligência;
- postura relaxada, braços livres e contato visual.

Paleta principal: cabelo `#24162F`, meio-tom `#3A214A`, mechas iluminadas `#59356C`, reflexos azul-violeta `#65417A` e sombras `#160F20`; olhos/tecnologia `#164E63` e `#22D3EE`; roupa `#171A1F` e `#0C1117`. O cabelo permanece 90–95% violeta/ameixa escuro e a mecha teal/ciano ocupa somente 5–10%. Nunca usar magenta, rosa choque, violeta neon ou brilho RGB.

Em luz clara, as sombras ameixa preservam o volume sem transformar o cabelo em roxo saturado. Em fundos escuros, os meios-tons e reflexos frios separam a silhueta; qualquer sombra visual deve acompanhar o alpha da personagem, nunca formar caixa. Olhos continuam teal e o Neural Link continua grafite/ciano.

## Arte ativa

`bust/nyra-bust-violet.png` é a representação oficial do Desktop Presence: cabeça, ombros e tórax superior, alpha real e âncora na base do busto. `portrait/nyra-portrait-violet-rgba.png` atende ao dashboard; `desktop/nyra-full-violet-rgba.png` preserva Full Body como variante secundária. Todos usam a mesma identidade violeta/teal. Ainda não constituem um rig profissional segmentado; olhos, boca e atividade do Neural Link continuam como overlays vetoriais do LayeredRenderer.

O V2 em /avatar/nyra.svg permanece como fallback operacional.

## Movimento

Idle usa respiração lenta, blink suave, microdeslocamento de olhar e movimento mínimo. Como o cabelo ainda não é uma camada isolada, não se simula reflexo com retângulo sobre o avatar. Listening acende o Neural Link; thinking pulsa e ativa topologia dos olhos; speaking reage ao áudio; concerned reduz brilho; amused usa sorriso lateral contido. O framing e a cor do cabelo não mudam entre estados. Nada deve ser frenético.
