[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$runtimeRoot = if ($env:NYRA_DATA_HOME) { $env:NYRA_DATA_HOME } else { Join-Path $env:LOCALAPPDATA 'NYRA' }
$modelDir = Join-Path $runtimeRoot 'data\models'
New-Item -ItemType Directory -Path $modelDir -Force | Out-Null
$assets = @(
    @{ Name='kokoro-v1.0.int8.onnx'; Url='https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx'; Hash='6E742170D309016E5891A994E1CE1559C702A2CCD0075E67EF7157974F6406CB' },
    @{ Name='voices-v1.0.bin'; Url='https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin'; Hash='BCA610B8308E8D99F32E6FE4197E7EC01679264EFED0CAC9140FE9C29F1FBF7D' }
)
foreach ($asset in $assets) {
    $target = Join-Path $modelDir $asset.Name
    if ((Test-Path -LiteralPath $target) -and (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -eq $asset.Hash) {
        Write-Host ('Reutilizando modelo TTS: ' + $asset.Name)
        continue
    }
    Write-Host ('Baixando modelo TTS necessário: ' + $asset.Name)
    $temporary = $target + '.download'
    Invoke-WebRequest -Uri $asset.Url -OutFile $temporary -UseBasicParsing
    if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash -ne $asset.Hash) { throw ('Hash inválido: ' + $asset.Name) }
    Move-Item -LiteralPath $temporary -Destination $target -Force
}
