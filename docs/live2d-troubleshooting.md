# Live2D troubleshooting

- `API_DISABLED`: abra VTS e habilite `Allow Plugin API access`.
- `AUTH_REQUIRED`: clique AUTHORIZE e aprove o popup.
- `MODEL_MISSING`: instale/exporte a NYRA e carregue o modelo.
- zero parâmetros: confirme modelo carregado e API autenticada.
- mouth parado: habilite Final Audio Lip Sync e verifique `ParamMouthOpenY`/`MouthOpen` descoberto.
- desconexão: confira host/porta e firewall local; Current Renderer continua disponível.
- export inválido: execute `scripts/validate-live2d-export.ps1`.
