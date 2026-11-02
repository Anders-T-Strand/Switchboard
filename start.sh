#!/usr/bin/env bash
# Sets up Switchboard on first run, rebuilds the dashboard when its source changed, then starts it.
# macOS and Linux. Usage: ./start.sh [--open]
set -euo pipefail

root="$(cd "$(dirname "$0")" && pwd)"
venv="$root/backend/.venv"
py="$venv/bin/python"
requirements="$root/backend/requirements.txt"
stamp="$venv/.requirements-installed"

if [ ! -x "$py" ]; then
    if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        echo "Switchboard needs Python 3.11 or newer (python3 is $(python3 --version 2>&1))." >&2
        exit 1
    fi
    echo "Creating Python environment..."
    python3 -m venv "$venv"
    "$py" -m pip install --upgrade pip
fi

# Reinstall Python packages when requirements.txt is newer than the last install.
if [ ! -f "$stamp" ] || [ "$requirements" -nt "$stamp" ]; then
    echo "Installing Python packages..."
    "$py" -m pip install -r "$requirements"
    touch "$stamp"
fi

# Rebuild the dashboard when it hasn't been built or a source file is newer than the build.
frontend="$root/frontend"
dist="$frontend/dist/index.html"
if [ ! -f "$dist" ] || [ -n "$(find "$frontend" \( -path "$frontend/node_modules" -o -path "$frontend/dist" \) -prune \
        -o -type f -newer "$dist" -print | head -n 1)" ]; then
    echo "Building the dashboard..."
    (cd "$frontend" && npm ci && npm run build)
fi

# Start Qdrant in Docker when config.yaml points the vector index at a local server that isn't running.
# Named volume for the data, and only listening on 127.0.0.1 since Qdrant has no login by default.
qdrant_url="$(grep -E '^[[:space:]]+url:[[:space:]]*http://(127\.0\.0\.1|localhost):[0-9]+' "$root/config.yaml" \
    | head -n 1 | sed -E 's/.*url:[[:space:]]*//; s/[[:space:]].*//' || true)"
if [ -n "$qdrant_url" ] && ! curl -fsS -m 2 "$qdrant_url/readyz" >/dev/null 2>&1; then
    if command -v docker >/dev/null 2>&1; then
        echo "Starting Qdrant in Docker..."
        port="${qdrant_url##*:}"
        docker start switchboard-qdrant >/dev/null 2>&1 \
            || docker run -d --name switchboard-qdrant --restart unless-stopped -p "127.0.0.1:$port:6333" \
                -v switchboard_qdrant:/qdrant/storage qdrant/qdrant >/dev/null 2>&1 \
            || true
        for _ in $(seq 1 30); do
            curl -fsS -m 2 "$qdrant_url/readyz" >/dev/null 2>&1 && break
            sleep 1
        done
        curl -fsS -m 2 "$qdrant_url/readyz" >/dev/null 2>&1 \
            || echo "Qdrant didn't start. Search by meaning, memory and learning from ratings stay off until it's up."
    else
        echo "Qdrant isn't running at $qdrant_url and Docker isn't installed. See the README for other ways to run it."
        echo "The dashboard works without it, minus search by meaning, memory and learning from ratings."
    fi
fi

cd "$root/backend"
exec "$py" -m switchboard "$@"
