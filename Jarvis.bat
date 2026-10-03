@echo off
rem Run Jarvis from source (fallback if Jarvis.exe has problems)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0jarvis.pyw" %*
