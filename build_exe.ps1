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

& $Py -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name Jarvis `
    --icon jarvis.ico `
    --add-data "models;models" `
    --add-data "web;web" `
    --hidden-import pystray._win32 `
    --hidden-import clr `
    --collect-all vosk `
    --exclude-module matplotlib --exclude-module scipy --exclude-module pandas `
    --exclude-module tkinter --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide6 --exclude-module gi `
    jarvis.pyw
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

$Exe = Join-Path $ProjectDir "dist\Jarvis.exe"
Copy-Item -LiteralPath $Exe -Destination (Join-Path $ProjectDir "Jarvis.exe") -Force
Write-Host "Built:  $Exe"
Write-Host "Copied: $(Join-Path $ProjectDir 'Jarvis.exe')"
