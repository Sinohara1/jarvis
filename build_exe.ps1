# Build Jarvis.exe (PyInstaller onefile, windowed) with the project venv.
# Usage: powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 [-Setup]
param([switch]$Setup)
$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

$Py = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) {
    Write-Host "Creating venv (.venv)..."
    $base = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $base) { $base = Join-Path $env:USERPROFILE "anaconda3\python.exe" }
    & $base -m venv .venv
    $Setup = $true
}
if ($Setup) {
    & $Py -m pip install --upgrade pip
    & $Py -m pip install -r requirements.txt
}

if (-not (Test-Path -LiteralPath "jarvis.ico")) {
    & $Py -c "import sys; sys.path.insert(0, '.'); from jarvis_app.tray import make_icon_image as m; m(256).save('jarvis.ico', sizes=[(16,16),(32,32),(48,48),(256,256)])"
}

# Piper (local TTS): bundle the full espeak-ng data (~20 MB, ~10 MB compressed) so voices of any language
# work, including the user's own voices (v1.4).
$PiperPkg = (& $Py -c "import piper, os; print(os.path.dirname(piper.__file__))").Trim()
if ($LASTEXITCODE -ne 0 -or -not $PiperPkg) { throw "piper-tts is not installed in .venv (pip install -r requirements.txt)" }
$EspeakSrc = Join-Path $PiperPkg "espeak-ng-data"
$EspeakDst = Join-Path $ProjectDir "build_assets\piper_espeak\espeak-ng-data"
if (Test-Path -LiteralPath $EspeakDst) { Remove-Item -LiteralPath $EspeakDst -Recurse -Force }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $EspeakDst) | Out-Null
Copy-Item -LiteralPath $EspeakSrc -Destination $EspeakDst -Recurse

& $Py -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name Jarvis `
    --icon jarvis.ico `
    --add-data "models;models" `
    --add-data "web;web" `
    --hidden-import pystray._win32 `
    --hidden-import clr `
    --collect-all vosk `
    --add-data "build_assets\piper_espeak\espeak-ng-data;piper_data\espeak-ng-data" `
    --hidden-import piper.espeakbridge `
    --exclude-module piper.train --exclude-module piper.http_server --exclude-module onnx `
    --exclude-module piper.phonemize_chinese --exclude-module piper.phonemize_japanese `
    --exclude-module piper.phonemize_thai --exclude-module piper.phonemize_hebrew --exclude-module piper.g2pw_onnx `
    --exclude-module matplotlib --exclude-module scipy --exclude-module pandas `
    --exclude-module tkinter --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide6 --exclude-module gi `
    jarvis.pyw
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

$Exe = Join-Path $ProjectDir "dist\Jarvis.exe"
Copy-Item -LiteralPath $Exe -Destination (Join-Path $ProjectDir "Jarvis.exe") -Force
Write-Host "Built:  $Exe"
Write-Host "Copied: $(Join-Path $ProjectDir 'Jarvis.exe')"
