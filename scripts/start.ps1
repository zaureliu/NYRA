[CmdletBinding()]
param()

# Compatibilidade: o entrypoint oficial de desenvolvimento e `npm run dev`
# na raiz, e ambos convergem para o mesmo bootstrap idempotente.
& (Join-Path $PSScriptRoot 'bootstrap.ps1') -Mode Development
exit $LASTEXITCODE
