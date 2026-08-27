[CmdletBinding()]
param([string]$ExportPath = '')
$ErrorActionPreference='Stop'
if(-not $ExportPath){$ExportPath=Join-Path $PSScriptRoot '..\live2d\exported'}
$root=(Resolve-Path -LiteralPath $ExportPath).Path
$model=Get-ChildItem -LiteralPath $root -Filter '*.model3.json' -File | Select-Object -First 1
if(-not $model){throw 'MODEL_MISSING: nenhum *.model3.json encontrado.'}
$json=Get-Content -LiteralPath $model.FullName -Raw | ConvertFrom-Json
$refs=@($json.FileReferences.Moc,$json.FileReferences.Physics)+@($json.FileReferences.Textures)+@($json.FileReferences.Expressions|ForEach-Object{$_.File})+@($json.FileReferences.Motions.PSObject.Properties.Value|ForEach-Object{$_}|ForEach-Object{$_.File})
$missing=@()
foreach($relative in $refs|Where-Object{$_}){$target=[IO.Path]::GetFullPath((Join-Path $model.DirectoryName $relative));if(-not $target.StartsWith($root,[StringComparison]::OrdinalIgnoreCase)-or -not(Test-Path -LiteralPath $target)){$missing+=$relative}}
if($missing){throw ('Referências ausentes/inseguras: '+($missing -join ', '))}
[pscustomobject]@{Valid=$true;Model=$model.Name;Root=$root;References=$refs.Count}|ConvertTo-Json
