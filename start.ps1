# Sets up Switchboard on first run, rebuilds the dashboard when its source changed, then starts it.
# Usage: powershell -ExecutionPolicy Bypass -File .\start.ps1

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$venv = Join-Path $root "backend\.venv"
$python = Join-Path $venv "Scripts\python.exe"
$requirements = Join-Path $root "backend\requirements.txt"
$stamp = Join-Path $venv ".requirements-installed"

if (-not (Test-Path $python)) {
    Write-Host "Creating Python environment..."
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv $venv } else { python -m venv $venv }
    & $python -m pip install --upgrade pip
}

# Reinstall Python packages when requirements.txt is newer than the last install.
if (-not (Test-Path $stamp) -or (Get-Item $requirements).LastWriteTime -gt (Get-Item $stamp).LastWriteTime) {
    Write-Host "Installing Python packages..."
    & $python -m pip install -r $requirements
    New-Item -ItemType File -Force -Path $stamp | Out-Null
}

# Rebuild the dashboard when it hasn't been built or any source file is newer than the build (e.g. after git pull).
$frontend = Join-Path $root "frontend"
$dist = Join-Path $frontend "dist\index.html"
$needBuild = -not (Test-Path $dist)
if (-not $needBuild) {
    $builtAt = (Get-Item $dist).LastWriteTime
    $newer = Get-ChildItem $frontend -Recurse -File |
        Where-Object { $_.FullName -notmatch '\\(node_modules|dist)\\' -and $_.LastWriteTime -gt $builtAt } |
        Select-Object -First 1
    $needBuild = $null -ne $newer
}
if ($needBuild) {
    Write-Host "Building the dashboard..."
    Push-Location $frontend
    try {
        npm ci  # installs exactly what package-lock.json says, without rewriting it
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
