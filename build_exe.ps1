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

# Optional Whisper STT (v1.5.x): bundled only when faster-whisper is installed in .venv
#   & .venv\Scripts\python.exe -m pip install "faster-whisper>=1.1"
# Adds ~60-100 MB to the exe (ctranslate2 + PyAV/ffmpeg + tokenizers). Models and the CUDA (cuBLAS)
# DLLs are NOT bundled - the app downloads them on demand into %LOCALAPPDATA%\Jarvis\whisper.
$WhisperArgs = @()
& $Py -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('faster_whisper') and u.find_spec('ctranslate2') else 1)"
if ($LASTEXITCODE -eq 0) {
    Write-Host "faster-whisper found - bundling the optional Whisper engine"
    $WhisperArgs = @("--collect-all", "faster_whisper", "--collect-all", "ctranslate2",
                     "--collect-binaries", "av", "--collect-submodules", "av", "--hidden-import", "av",
                     "--collect-all", "tokenizers", "--hidden-import", "huggingface_hub")
} else {
    Write-Host "faster-whisper not installed - building without Whisper (Vosk only)"
}

# v1.6 "hear PC audio": WASAPI loopback via PyAudioWPatch (small, ~1 MB). Installed automatically if missing.
& $Py -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('pyaudiowpatch') else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyAudioWPatch (PC audio loopback)..."
    & $Py -m pip install --disable-pip-version-check -q PyAudioWPatch
}
$LoopbackArgs = @()
& $Py -c "import pyaudiowpatch"
if ($LASTEXITCODE -eq 0) {
    Write-Host "PyAudioWPatch found - bundling PC audio hearing"
    $LoopbackArgs = @("--hidden-import", "pyaudiowpatch", "--collect-binaries", "pyaudiowpatch")
} else {
    Write-Host "PyAudioWPatch not available - building without PC audio hearing"
}

& $Py -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name Jarvis `
    --icon jarvis.ico `
    --add-data "models;models" `
    --add-data "web;web" `
    --add-data "phone;phone" `
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
    @WhisperArgs `
    @LoopbackArgs `
    jarvis.pyw
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

$Exe = Join-Path $ProjectDir "dist\Jarvis.exe"
Copy-Item -LiteralPath $Exe -Destination (Join-Path $ProjectDir "Jarvis.exe") -Force
Write-Host "Built:  $Exe"
Write-Host "Copied: $(Join-Path $ProjectDir 'Jarvis.exe')"
