# Sets up Switchboard on first run, then starts it.
# Usage: powershell -ExecutionPolicy Bypass -File .\start.ps1

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$venv = Join-Path $root "backend\.venv"
$python = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Creating Python environment..."
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv $venv } else { python -m venv $venv }
    & $python -m pip install --upgrade pip
    & $python -m pip install -r (Join-Path $root "backend\requirements.txt")
}

if (-not (Test-Path (Join-Path $root "frontend\dist\index.html"))) {
    Write-Host "Building the dashboard..."
    Push-Location (Join-Path $root "frontend")
    try {
        npm install
        npm run build
    } finally {
        Pop-Location
    }
}

Push-Location (Join-Path $root "backend")
try {
    & $python -m switchboard @args
} finally {
    Pop-Location
}
