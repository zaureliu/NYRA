"""OCR fallback via Windows.Media.Ocr (WinRT) through PowerShell (§20).

Structured APIs remain first choice (§19); this path only materializes an
already-captured frame to a temp PNG and asks the OS OCR engine for lines.
Runs fully local; fails honestly with available=False when unsupported.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

_SCRIPT_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
$path = $args[0]
try {
  [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
  [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
  [Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
  [Windows.Storage.Streams.IRandomAccessStream, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
  [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null

  function Await($WinRtTask, $ResultType) {
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
  }

  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if ($null -eq $engine) { Write-Output ('{"available": false, "note": "sem idioma OCR instalado"}'); exit 0 }

  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

  $lines = @()
  foreach ($line in $result.Lines) {
    $b = $line.BoundingRect
    $lines += , @($line.Text, [int]$b.X, [int]$b.Y, [int]$b.Width, [int]$b.Height)
  }
  $payload = @{ available = $true; lines = $lines } | ConvertTo-Json -Compress -Depth 4
  Write-Output $payload
} catch {
  Write-Output ('{"available": false, "note": "' + $_.Exception.Message.Replace('"', "'") + '"}')
}
"""

_LINE_JSON = re.compile(r"^\s*[\[{]")


def windows_ocr_available() -> bool:
    """Cheap capability probe (does the OS expose OcrEngine at all?)."""
    try:
        completed = subprocess.run(  # noqa: S603
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             "[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null; "
             "$e=[Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages(); "
             "if ($null -ne $e) { 'OK' } else { 'NO_ENGINE' }"],
            capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return b"OK" in completed.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_windows_ocr(png_path: str, timeout_seconds: float = 30.0) -> dict:
    script = Path(tempfile.gettempdir()) / f"nyra-ocr-{abs(hash(png_path)) % 99999}.ps1"
    script.write_text(_SCRIPT_TEMPLATE, encoding="utf-8-sig")
    try:
        completed = subprocess.run(  # noqa: S603
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", str(script), str(png_path)],
            capture_output=True, timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        for line in stdout.splitlines():
            if _LINE_JSON.match(line):
                document = json.loads(line)
                if not document.get("available"):
                    return {"available": False, "lines": [], "note": document.get("note", "")}
                regions = []
                for text, x, y, width, height in document.get("lines") or []:
                    regions.append((str(text), {"x": int(x), "y": int(y),
                                                "width": int(width), "height": int(height)}))
                return {"available": True, "lines": regions}
        return {"available": False, "lines": [], "note": "OCR não retornou JSON."}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "lines": [], "note": f"{type(exc).__name__}"}
    finally:
        try:
            script.unlink(missing_ok=True)
        except OSError:
            pass
