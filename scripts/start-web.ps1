[CmdletBinding()]
param()

# Compatibilidade com atalhos antigos: o painel agora e empacotado no Tauri.
& (Join-Path $PSScriptRoot 'start-kazumi.ps1')
