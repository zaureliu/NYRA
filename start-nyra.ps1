[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path $PSScriptRoot).Path
# The shortcut starts the canonical Python backend before the release desktop.
# Tauri then reuses the healthy loopback service instead of depending on a
# PyInstaller executable that endpoint protection may quarantine heuristically.
# The packaged sidecar remains built and independently validated for portable
# release use; this local-first launcher never weakens endpoint protection.
& (Join-Path $repoRoot 'scripts\start-nyra.ps1')
exit $LASTEXITCODE
