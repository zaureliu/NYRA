# Avatar Controller

`AvatarController` é a camada de alto nível para emoção, olhos, boca, cabeça, corpo, respiração, Neural Link e animações. O renderer atual recebe valores contínuos como `eye_x`, `eye_y`, `head_x`, `head_y`, `head_tilt`, `body_x`, `breathing`, `mouth_open` e `expression_weight`; o `LayeredRenderer` interpola esses valores com transforms CSS.

Busto/retrato, alpha real, lip sync, blink, estados e fallbacks atuais são preservados. Listening reduz movimento e ativa o Neural Link; thinking usa foco/head tilt; speaking preserva lip sync; alert usa concerned sem efeitos exagerados.

Providers visuais seguem um contrato comum: renderer atual, layered e adapter futuro de VTube Studio. O adapter não instala software nem injeta parâmetros sem autenticação. A preparação segue a [API pública oficial do VTube Studio](https://github.com/DenchiSoft/VTubeStudio): WebSocket local, permissão do usuário e atualização contínua dos parâmetros controlados. Neste host o executável foi detectado, mas não existe modelo Live2D da NYRA no repositório nem token/API configurado; o renderer atual permanece o fallback funcional.
