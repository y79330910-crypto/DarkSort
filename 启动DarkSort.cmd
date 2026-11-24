@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
setlocal EnableDelayedExpansion
if exist "dist\launcher-version.txt" (
  set "releaseVersion="
  set /p "releaseVersion="<"dist\launcher-version.txt"
  for /f "delims=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.+-" %%V in ("v!releaseVersion!") do set "releaseVersion="
  if defined releaseVersion if exist "dist\releases\!releaseVersion!\DarkSort\DarkSort.exe" (
    start "" "dist\releases\!releaseVersion!\DarkSort\DarkSort.exe"
    exit /b
  )
)
if exist "dist\DarkSort\DarkSort.exe" (
  start "" "dist\DarkSort\DarkSort.exe"
  exit /b
)
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -m darksort
  exit /b
)
echo DarkSort environment not found. See README.md for setup.
pause
