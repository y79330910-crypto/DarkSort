param(
    [switch]$Installer,
    [string]$InnoCompiler
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
$distDir = Join-Path $projectRoot 'dist'

if ($Installer) {
    if (-not $InnoCompiler) {
        $InnoCompiler = (Get-Command ISCC.exe -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1).Source
    }
    if (-not $InnoCompiler) {
        $InnoCompiler = @(
            "$env:LOCALAPPDATA/Programs/Inno Setup 6/ISCC.exe"
            "${env:ProgramFiles(x86)}/Inno Setup 6/ISCC.exe"
        ) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    }
    if (-not $InnoCompiler) {
        throw 'Install Inno Setup 6.7 or specify its ISCC.exe with -InnoCompiler.'
    }
    $InnoCompiler = (Resolve-Path -LiteralPath $InnoCompiler).Path
}

$appVersion = (& $pythonExe -c 'from darksort import __version__; print(__version__)').Trim()
if ($appVersion -notmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)*$') {
    throw "Invalid DarkSort version: $appVersion"
}
$releaseDir = Join-Path $distDir "releases/$appVersion"
$bundleDir = Join-Path $releaseDir 'DarkSort'
$bundleExe = Join-Path $bundleDir 'DarkSort.exe'
if (Get-Process -Name DarkSort -ErrorAction SilentlyContinue | Where-Object Path -eq $bundleExe) {
    throw "Close the running release in $bundleDir before rebuilding it."
}
$originalPath = $env:Path
$originalCacheDir = $env:PYINSTALLER_CONFIG_DIR

Push-Location -LiteralPath $projectRoot
try {
    # Qt links against Windows ICU. Other tool directories can contain incompatible DLLs with the same name.
    $env:Path = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $projectRoot 'build/pyinstaller-cache'
    & $pythonExe -m PyInstaller --noconfirm --clean --windowed --onedir --name DarkSort --paths src `
        --distpath $releaseDir `
        --icon src/darksort/assets/darksort.ico `
        --add-data src/darksort/assets/darksort.ico:darksort/assets `
        --exclude-module PySide6.QtNetwork --exclude-module PySide6.QtQml `
        --exclude-module PySide6.QtQuick scripts/launch.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

    # The packaged entry point loads Qt before processing --help.
    $startupCheck = Start-Process -FilePath $bundleExe `
        -ArgumentList '--help' -PassThru -WindowStyle Hidden
    try {
        if (-not $startupCheck.WaitForExit(15000)) {
            throw 'Packaged DarkSort startup check timed out.'
        }
        if ($startupCheck.ExitCode -ne 0) {
            throw "Packaged DarkSort startup check failed (exit code $($startupCheck.ExitCode))."
        }
    } finally {
        if (-not $startupCheck.HasExited) {
            $startupCheck.Kill()
            [void]$startupCheck.WaitForExit(3000)
        }
        $startupCheck.Dispose()
    }

    Copy-Item -LiteralPath "$projectRoot/packaging/使用说明.txt" -Destination $bundleDir
    $zipPath = Join-Path $distDir "DarkSort-$appVersion-windows-x64.zip"
    Compress-Archive -LiteralPath $bundleDir -DestinationPath $zipPath -Force

    if ($Installer) {
        & $InnoCompiler "/DAppVersion=$appVersion" "$projectRoot/packaging/DarkSort.iss"
        if ($LASTEXITCODE -ne 0) { throw 'Windows installer build failed.' }
    }

    # Publish the launcher target after the requested release artifacts are ready.
    $launcherTarget = Join-Path $distDir 'launcher-version.txt'
    $temporaryTarget = Join-Path $distDir ([IO.Path]::GetRandomFileName())
    try {
        [IO.File]::WriteAllText($temporaryTarget, "$appVersion`r`n", [Text.Encoding]::ASCII)
        if ([IO.File]::Exists($launcherTarget)) {
            [IO.File]::Replace($temporaryTarget, $launcherTarget, [NullString]::Value)
        } else {
            [IO.File]::Move($temporaryTarget, $launcherTarget)
        }
    } finally {
        [IO.File]::Delete($temporaryTarget)
    }
} finally {
    $env:Path = $originalPath
    $env:PYINSTALLER_CONFIG_DIR = $originalCacheDir
    Pop-Location
}
