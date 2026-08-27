[CmdletBinding()]
param([string]$ExportPath='')
if(-not $ExportPath){$ExportPath=Join-Path $PSScriptRoot '..\live2d\exported'}
$resolved=(Resolve-Path -LiteralPath $ExportPath).Path
Write-Host "Aguardando export em $resolved (Ctrl+C cancela)."
$watcher=[IO.FileSystemWatcher]::new($resolved,'*.model3.json');$watcher.EnableRaisingEvents=$true
try{while($true){$event=$watcher.WaitForChanged([IO.WatcherChangeTypes]::Created -bor [IO.WatcherChangeTypes]::Changed,30000);if(-not $event.TimedOut){& (Join-Path $PSScriptRoot 'validate-live2d-export.ps1') -ExportPath $resolved;break}}}finally{$watcher.Dispose()}
