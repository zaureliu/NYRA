[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$outputDirectory = Join-Path $repoRoot 'docs\screenshots\avatar-v2'
$items = @(
  [pscustomobject]@{ Label='IDLE'; File='nyra-avatar-v2-idle.png' },
  [pscustomobject]@{ Label='EYES OPEN'; File='nyra-avatar-v2-idle.png' },
  [pscustomobject]@{ Label='BLINK HALF'; File='nyra-avatar-v2-blink-half.png' },
  [pscustomobject]@{ Label='BLINK CLOSED'; File='nyra-avatar-v2-blink-closed.png' },
  [pscustomobject]@{ Label='MOUTH CLOSED'; File='nyra-avatar-v2-idle.png' },
  [pscustomobject]@{ Label='MOUTH SMALL'; File='nyra-avatar-v2-speaking-small.png' },
  [pscustomobject]@{ Label='MOUTH MEDIUM'; File='nyra-avatar-v2-speaking-medium.png' },
  [pscustomobject]@{ Label='MOUTH OPEN'; File='nyra-avatar-v2-speaking-open.png' },
  [pscustomobject]@{ Label='LISTENING'; File='nyra-avatar-v2-listening.png' },
  [pscustomobject]@{ Label='THINKING'; File='nyra-avatar-v2-thinking.png' },
  [pscustomobject]@{ Label='MOBILE 390'; File='nyra-avatar-v2-mobile.png' }
)

$columnCount = 4
$tileWidth = 380
$tileHeight = 500
$rowCount = [int][Math]::Ceiling($items.Count / [double]$columnCount)
$sheet = [System.Drawing.Bitmap]::new($tileWidth * $columnCount, $tileHeight * $rowCount)
$graphics = [System.Drawing.Graphics]::FromImage($sheet)
$font = [System.Drawing.Font]::new('Segoe UI', 13, [System.Drawing.FontStyle]::Bold)
$labelBrush = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(213, 224, 255))
$borderPen = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(35, 52, 92), 2)
try {
  $graphics.Clear([System.Drawing.Color]::FromArgb(5, 9, 23))
  $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
  $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
  for ($index = 0; $index -lt $items.Count; $index++) {
    $column = $index % $columnCount
    $row = [int][Math]::Floor($index / [double]$columnCount)
    $x = $column * $tileWidth
    $y = $row * $tileHeight
    $graphics.DrawRectangle($borderPen, $x + 7, $y + 7, $tileWidth - 14, $tileHeight - 14)
    $graphics.DrawString($items[$index].Label, $font, $labelBrush, $x + 20, $y + 17)
    $source = [System.Drawing.Image]::FromFile((Join-Path $outputDirectory $items[$index].File))
    try {
      $availableWidth = $tileWidth - 28
      $availableHeight = $tileHeight - 62
      $scale = [Math]::Min($availableWidth / $source.Width, $availableHeight / $source.Height)
      $drawWidth = [int]($source.Width * $scale)
      $drawHeight = [int]($source.Height * $scale)
      $drawX = $x + [int](($tileWidth - $drawWidth) / 2)
      $drawY = $y + 48 + [int](($availableHeight - $drawHeight) / 2)
      $graphics.DrawImage($source, $drawX, $drawY, $drawWidth, $drawHeight)
    } finally { $source.Dispose() }
  }
  $contactSheet = Join-Path $outputDirectory 'nyra-avatar-v2-contact-sheet.png'
  $sheet.Save($contactSheet, [System.Drawing.Imaging.ImageFormat]::Png)
  Write-Output $contactSheet
} finally {
  $borderPen.Dispose()
  $labelBrush.Dispose()
  $font.Dispose()
  $graphics.Dispose()
  $sheet.Dispose()
}
