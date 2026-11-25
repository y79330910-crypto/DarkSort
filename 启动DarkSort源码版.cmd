@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo DarkSort source environment not found. Run uv sync --extra dev first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m darksort %*
