[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProcessName,
    [Parameter(Mandatory=$true)][string]$Output
)

$ErrorActionPreference = 'Stop'
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class KazumiAppWindowCapture {
  [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left, Top, Right, Bottom; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out Rect rect);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int command);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
'@
Add-Type -AssemblyName System.Drawing

$process = Get-Process -Name $ProcessName -ErrorAction Stop | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $process) { throw "Janela do processo $ProcessName nao encontrada." }
[void][KazumiAppWindowCapture]::ShowWindow($process.MainWindowHandle, 9)
[void][KazumiAppWindowCapture]::SetForegroundWindow($process.MainWindowHandle)
Start-Sleep -Milliseconds 350
$rect = New-Object KazumiAppWindowCapture+Rect
if (-not [KazumiAppWindowCapture]::GetWindowRect($process.MainWindowHandle, [ref]$rect)) { throw 'Nao foi possivel obter os limites da janela.' }
$width = $rect.Right - $rect.Left; $height = $rect.Bottom - $rect.Top
if ($width -lt 1 -or $height -lt 1) { throw 'Janela sem area visivel.' }
$directory = Split-Path -Parent $Output
if ($directory) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
$bitmap = New-Object Drawing.Bitmap $width, $height
$graphics = [Drawing.Graphics]::FromImage($bitmap)
try {
    $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bitmap.Size)
    $bitmap.Save($Output, [Drawing.Imaging.ImageFormat]::Png)
} finally {
    $graphics.Dispose(); $bitmap.Dispose()
}
Write-Output ("captured={0}x{1} pid={2}" -f $width, $height, $process.Id)
