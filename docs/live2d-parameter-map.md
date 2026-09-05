# Live2D parameter map

O manifesto desejado fica em `live2d/manifests/kazumi_parameters.json`; candidatos de transporte ficam em `config/vtube_parameter_mapping.yaml`. O provider cruza candidatos com os IDs realmente retornados pelo VTS e não inventa parâmetros.

Standard: angles, eye ball, mouth open/form, body e breath. Mouse tracking prefere `EyeBallX/Y` e `FaceAngleX/Y`; cada eixo ausente é ignorado isoladamente. Custom opcionais de emoção só são usados depois de existirem no modelo atual e na descoberta.
