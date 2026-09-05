[CmdletBinding(SupportsShouldProcess)]
param([string]$ExportPath='')
$ErrorActionPreference='Stop'
if(-not $ExportPath){$ExportPath=Join-Path $PSScriptRoot '..\live2d\exported'}
& (Join-Path $PSScriptRoot 'validate-live2d-export.ps1') -ExportPath $ExportPath | Out-Null
$vts='C:\Program Files (x86)\Steam\steamapps\common\VTube Studio\VTube Studio_Data\StreamingAssets\Live2DModels'
if(-not(Test-Path -LiteralPath $vts)){throw 'ACTION REQUIRED: Install VTube Studio ou informe uma instalação oficial válida.'}
$source=(Resolve-Path -LiteralPath $ExportPath).Path;$destination=Join-Path $vts 'KAZUMI';$backup=Join-Path $vts ('KAZUMI.backup.'+(Get-Date -Format 'yyyyMMdd-HHmmss'))
if(Test-Path -LiteralPath $destination){if($PSCmdlet.ShouldProcess($destination,"backup para $backup")){Move-Item -LiteralPath $destination -Destination $backup}}
if($PSCmdlet.ShouldProcess($destination,'instalar export Live2D validado')){Copy-Item -LiteralPath $source -Destination $destination -Recurse}
[pscustomobject]@{Installed=$true;Destination=$destination;Backup=(Test-Path -LiteralPath $backup)}|ConvertTo-Json
