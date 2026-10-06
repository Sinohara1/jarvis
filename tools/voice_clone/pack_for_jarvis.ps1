# Zip a trained Piper voice (name.onnx + name.onnx.json) for Jarvis import.
# Jarvis: Голос → загрузить свой голос → выбрать .zip (или оба файла).
# Import path: %LOCALAPPDATA%\Jarvis\voices\<name>\
#
# Usage:
#   .\pack_for_jarvis.ps1 -Onnx "C:\path\myvoice.onnx"
#   .\pack_for_jarvis.ps1 -Onnx ".\myvoice.onnx" -Json ".\myvoice.onnx.json" -Out ".\myvoice_jarvis.zip"
#   .\pack_for_jarvis.ps1 -Dir "C:\path\to\folder"   # picks the only .onnx + matching json

[CmdletBinding()]
param(
    [string]$Onnx = "",
    [string]$Json = "",
    [string]$Dir = "",
    [string]$Out = "",
    [string]$Name = ""
)

$ErrorActionPreference = "Stop"

function Resolve-VoicePair {
    param([string]$OnnxPath, [string]$JsonPath, [string]$Folder)

    if ($Folder) {
        $Folder = (Resolve-Path $Folder).Path
        $onnxFiles = @(Get-ChildItem -Path $Folder -Filter "*.onnx" -File | Where-Object { $_.Name -notlike "*.onnx.json" })
        if ($onnxFiles.Count -eq 0) { throw "В папке нет .onnx: $Folder" }
        if ($onnxFiles.Count -gt 1) { throw "В папке несколько .onnx — укажи -Onnx явно." }
        $OnnxPath = $onnxFiles[0].FullName
    }

    if (-not $OnnxPath) { throw "Укажи -Onnx путь к файлу .onnx или -Dir с одним голосом." }
    $OnnxPath = (Resolve-Path $OnnxPath).Path
    if ($OnnxPath -notmatch '\.onnx$') { throw "Ожидался файл .onnx: $OnnxPath" }

    if (-not $JsonPath) {
        $stem = [IO.Path]::GetFileNameWithoutExtension($OnnxPath)
        $dirn = Split-Path $OnnxPath -Parent
        $candidates = @(
            "$OnnxPath.json",
            (Join-Path $dirn "$stem.onnx.json"),
            (Join-Path $dirn "$stem.json")
        )
        foreach ($c in $candidates) {
            if (Test-Path $c) { $JsonPath = $c; break }
        }
    }
    if (-not $JsonPath -or -not (Test-Path $JsonPath)) {
        throw "Не найден .onnx.json рядом с моделью. Укажи -Json."
    }
    $JsonPath = (Resolve-Path $JsonPath).Path

    # Sanity: JSON looks like Piper config
    try {
        $cfg = Get-Content -Raw -Encoding UTF8 $JsonPath | ConvertFrom-Json
    } catch {
        throw "JSON не читается: $JsonPath"
    }
    if (-not $cfg.phoneme_id_map) { throw "Это не конфиг Piper (нет phoneme_id_map)." }
    if (-not $cfg.audio.sample_rate) { throw "Это не конфиг Piper (нет audio.sample_rate)." }

    return @{ Onnx = $OnnxPath; Json = $JsonPath }
}

$pair = Resolve-VoicePair -OnnxPath $Onnx -JsonPath $Json -Folder $Dir
$onnxFile = Get-Item $pair.Onnx
$base = if ($Name) { $Name } else { [IO.Path]::GetFileNameWithoutExtension($onnxFile.Name) }
# sanitize like Jarvis slug-ish
$safe = ($base -replace '[^\w\-.]+', '_').Trim('._-')
if (-not $safe) { $safe = "voice" }

if (-not $Out) {
    $Out = Join-Path $onnxFile.DirectoryName "${safe}_jarvis.zip"
}
$Out = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Out)

$stage = Join-Path ([IO.Path]::GetTempPath()) ("jarvis_voice_pack_" + [guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    $onnxDest = Join-Path $stage "$safe.onnx"
    $jsonDest = Join-Path $stage "$safe.onnx.json"
    Copy-Item $pair.Onnx $onnxDest -Force
    Copy-Item $pair.Json $jsonDest -Force
    if (Test-Path $Out) { Remove-Item $Out -Force }
    Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $Out -CompressionLevel Optimal
} finally {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Готово: $Out" -ForegroundColor Green
Write-Host "В Jarvis: Настройки → Голос → загрузить свой голос → выбери этот zip."
Write-Host "Файлы окажутся в: $env:LOCALAPPDATA\Jarvis\voices\$safe\"
Write-Host ""
Write-Host "Напоминание: этот скрипт только упаковывает уже обученный .onnx + .onnx.json."
Write-Host "Запись (record_voice) сама по себе голос не обучает."
