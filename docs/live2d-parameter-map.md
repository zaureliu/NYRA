# Live2D parameter map

O manifesto desejado fica em `live2d/manifests/nyra_parameters.json`; candidatos de transporte ficam em `config/vtube_parameter_mapping.yaml`. O provider cruza candidatos com os IDs realmente retornados pelo VTS e não inventa parâmetros.

Standard: angles, eye open/ball, brows, mouth open/form, body e breath. Custom opcionais: Neural Link, attention, thinking, concern e amused. Parâmetros custom só serão usados depois de existirem no rig exportado e na descoberta.
