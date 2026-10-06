# Guided recording for a custom Piper voice (Jarvis).
# Creates wavs/ + metadata.csv (LJSpeech). Does NOT produce .onnx.
# Usage (PowerShell):
#   cd ...\tools\voice_clone
#   .\record_voice.ps1
#   .\record_voice.ps1 -OutDir "$env:USERPROFILE\Desktop\JarvisVoiceRecord"
#   .\record_voice.ps1 -Start 120
#   .\record_voice.ps1 -Backend ffmpeg

[CmdletBinding()]
param(
    [string]$OutDir = "",
    [string]$Prompts = "",
    [ValidateSet("auto", "sounddevice", "ffmpeg")]
    [string]$Backend = "auto",
    [int]$Start = 1
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here

function Find-Python {
    foreach ($c in @(
        @{ File = "py"; Args = @("-3") },
        @{ File = "python"; Args = @() },
        @{ File = "python3"; Args = @() }
    )) {
        $cmd = Get-Command $c.File -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            $ver = & $cmd.Source @($c.Args + @("-c", "import sys; print(sys.version_info>= (3,9))")) 2>$null
            if ($ver -match "True") {
                return @{ Exe = $cmd.Source; Prefix = $c.Args }
            }
        } catch { }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "Нужен Python 3.9+ в PATH (python.org → Add to PATH)." -ForegroundColor Red
    exit 1
}

# Ensure sounddevice unless user forced ffmpeg
if ($Backend -ne "ffmpeg") {
    & $py.Exe @($py.Prefix + @("-c", "import sounddevice")) 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Ставлю sounddevice (один раз)…" -ForegroundColor Yellow
        & $py.Exe @($py.Prefix + @("-m", "pip", "install", "--user", "sounddevice", "numpy"))
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Не удалось установить sounddevice. Попробуй: pip install sounddevice" -ForegroundColor Red
            Write-Host "Или поставь ffmpeg и запусти: .\record_voice.ps1 -Backend ffmpeg" -ForegroundColor Yellow
            exit 1
        }
    }
}

$script = Join-Path $Here "record_voice.py"
if (-not (Test-Path $script)) {
    Write-Host "Не найден record_voice.py рядом со скриптом." -ForegroundColor Red
    exit 1
}

$argsList = @($script, "--backend", $Backend, "--start", "$Start")
if ($OutDir) { $argsList += @("--out", $OutDir) }
if ($Prompts) { $argsList += @("--prompts", $Prompts) }

Write-Host "Jarvis · запись датасета для Piper (это ещё не .onnx)" -ForegroundColor Cyan
& $py.Exe @($py.Prefix + $argsList)
exit $LASTEXITCODE
