[CmdletBinding()]
param([string]$OutputDirectory = '')

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'runtime-paths.ps1')
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path (Initialize-NyraRuntimePaths).Temp 'desktop-cursor-smoke'
}
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class NyraCursorSmokeNative {
    [StructLayout(LayoutKind.Sequential)] public struct Point { public int X; public int Y; }
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
}
'@

$resolvedOutput = [IO.Path]::GetFullPath((Join-Path (Get-Location) $OutputDirectory))
New-Item -ItemType Directory -Path $resolvedOutput -Force | Out-Null
$original = New-Object NyraCursorSmokeNative+Point
[NyraCursorSmokeNative]::GetCursorPos([ref]$original) | Out-Null
$captures = @()

try {
    $screens = @([System.Windows.Forms.Screen]::AllScreens)
    foreach ($screen in $screens) {
        $bounds = $screen.Bounds
        $points = [ordered]@{
            center = @(($bounds.Left + [int]($bounds.Width / 2)), ($bounds.Top + [int]($bounds.Height / 2)))
            top_left = @(($bounds.Left + 32), ($bounds.Top + 32))
            top_right = @(($bounds.Right - 33), ($bounds.Top + 32))
            bottom_left = @(($bounds.Left + 32), ($bounds.Bottom - 33))
            bottom_right = @(($bounds.Right - 33), ($bounds.Bottom - 33))
        }
        foreach ($entry in $points.GetEnumerator()) {
            [NyraCursorSmokeNative]::SetCursorPos($entry.Value[0], $entry.Value[1]) | Out-Null
            Start-Sleep -Milliseconds 420
            $safeDevice = ($screen.DeviceName -replace '[^A-Za-z0-9_-]', '_').Trim('_')
            $output = Join-Path $resolvedOutput "$safeDevice-$($entry.Key).png"
            & (Join-Path $PSScriptRoot 'capture-desktop-window.ps1') -Output $output | Out-Null
            $captures += [pscustomobject]@{
                monitor = $screen.DeviceName
                primary = $screen.Primary
                bounds = @{ x = $bounds.X; y = $bounds.Y; width = $bounds.Width; height = $bounds.Height }
                point = $entry.Key
                cursor = @{ x = $entry.Value[0]; y = $entry.Value[1] }
                capture = $output
            }
        }
    }
} finally {
    [NyraCursorSmokeNative]::SetCursorPos($original.X, $original.Y) | Out-Null
}

Start-Sleep -Milliseconds 2600
$neutralCapture = Join-Path $resolvedOutput 'return-neutral.png'
& (Join-Path $PSScriptRoot 'capture-desktop-window.ps1') -Output $neutralCapture | Out-Null
$report = [pscustomobject]@{
    monitors = @([System.Windows.Forms.Screen]::AllScreens).Count
    captures = $captures
    return_to_neutral = $neutralCapture
}
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $resolvedOutput 'report.json') -Encoding UTF8
$report | ConvertTo-Json -Depth 8
