[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Output, [switch]$Show)
$ErrorActionPreference = 'Stop'
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class NyraWindowCapture {
  [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left, Top, Right, Bottom; }
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int command);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out Rect rect);
}
'@
Add-Type -AssemblyName System.Drawing
$process = Get-Process -Name 'nyra-desktop' -ErrorAction Stop | Sort-Object StartTime -Descending | Select-Object -First 1
if (-not $process) { throw 'Janela nyra-desktop não encontrada.' }
if ($Show) { [void][NyraWindowCapture]::ShowWindow($process.MainWindowHandle, 5); Start-Sleep -Milliseconds 250 }
$windows = New-Object System.Collections.Generic.List[object]
$callback = [NyraWindowCapture+EnumWindowsProc]{
  param([IntPtr]$handle, [IntPtr]$unused)
  [uint32]$owner = 0
  [void][NyraWindowCapture]::GetWindowThreadProcessId($handle, [ref]$owner)
  if ($owner -eq $process.Id -and [NyraWindowCapture]::IsWindowVisible($handle)) {
    $candidate = New-Object NyraWindowCapture+Rect
    if ([NyraWindowCapture]::GetWindowRect($handle, [ref]$candidate)) {
      $area = ($candidate.Right - $candidate.Left) * ($candidate.Bottom - $candidate.Top)
      $windows.Add([pscustomobject]@{
        Handle=$handle
        Rect=$candidate
        Area=$area
        Width=($candidate.Right - $candidate.Left)
        Height=($candidate.Bottom - $candidate.Top)
      })
    }
  }
  return $true
}
[void][NyraWindowCapture]::EnumWindows($callback, [IntPtr]::Zero)
$selected = $windows |
  Where-Object { $_.Width -le 700 -and $_.Height -le 800 -and $_.Area -gt 10000 } |
  Sort-Object Area -Descending |
  Select-Object -First 1
if (-not $selected) { $selected = $windows | Sort-Object Area -Descending | Select-Object -First 1 }
if (-not $selected) { throw 'Nenhuma janela visível da NYRA encontrada.' }
$rect = $selected.Rect
$width = $rect.Right - $rect.Left
$height = $rect.Bottom - $rect.Top
$bitmap = New-Object System.Drawing.Bitmap $width, $height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
try {
  $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bitmap.Size)
  $bitmap.Save($Output, [System.Drawing.Imaging.ImageFormat]::Png)
} finally {
  $graphics.Dispose()
  $bitmap.Dispose()
}
Write-Output ("captured={0}x{1} pid={2} windows={3}" -f $width, $height, $process.Id, $windows.Count)
