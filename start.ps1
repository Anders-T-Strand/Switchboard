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

# Start Qdrant in Docker when config.yaml points the vector index at a local server that isn't running.
# Uses a named Docker volume, since Qdrant warns that Windows folder mounts can lose data.
$config = Get-Content (Join-Path $root "config.yaml") -Raw
$qdrant = [regex]::Match($config, '(?m)^\s+url:\s*(http://(?:127\.0\.0\.1|localhost):(\d+))')
if ($qdrant.Success -and $config -notmatch '(?m)^index:\s*\r?\n\s+enabled:\s*false') {
    $qdrantUrl = $qdrant.Groups[1].Value
    $port = $qdrant.Groups[2].Value
    function Test-Qdrant {
        try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "$qdrantUrl/readyz" | Out-Null; return $true }
        catch { return $false }
    }
    if (-not (Test-Qdrant)) {
        if (Get-Command docker -ErrorAction SilentlyContinue) {
            Write-Host "Starting Qdrant in Docker..."
            $ErrorActionPreference = "Continue"
            $existing = docker ps -a --filter "name=^switchboard-qdrant$" --format "{{.Names}}" 2>$null
            if ($existing) {
                docker start switchboard-qdrant 2>$null | Out-Null
            } else {
                docker run -d --name switchboard-qdrant --restart unless-stopped -p "${port}:6333" `
                    -v switchboard_qdrant:/qdrant/storage qdrant/qdrant 2>$null | Out-Null
            }
            $ErrorActionPreference = "Stop"
            $ready = $false
            foreach ($i in 1..30) { if (Test-Qdrant) { $ready = $true; break }; Start-Sleep -Seconds 1 }
            if (-not $ready) {
                Write-Host "Qdrant didn't start. Is Docker Desktop running? The dashboard works without it,"
                Write-Host "but search by meaning, memory and learning from ratings stay off until it's up."
            }
        } else {
            Write-Host "Qdrant isn't running at $qdrantUrl and Docker isn't installed. See the README for other"
            Write-Host "ways to run it. The dashboard works without it, minus search by meaning, memory and learning."
        }
    }
}

Push-Location (Join-Path $root "backend")
try {
    & $python -m switchboard @args
} finally {
    Pop-Location
}
