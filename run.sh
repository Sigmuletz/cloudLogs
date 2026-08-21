#!/usr/bin/env bash
# Start the cloudlogs viewer. Ingest runs automatically at startup when
# data/logs.json is missing or older than its inputs.
#
#   ./run.sh                          # http://localhost:8000
#   PORT=9000 ./run.sh
#   HOST=127.0.0.1 ./run.sh           # loopback only (see the note below)
#   CLOUDLOGS_INPUT='logs/**/*.log' ./run.sh
#
# The default bind is 0.0.0.0 so the server is reachable from outside the
# machine it runs on -- from Windows when this is a WSL distro, and from other
# hosts on the LAN. There is no authentication, so on an untrusted network set
# HOST=127.0.0.1. Windows/LAN details: see "Running outside WSL" in PLAN.md.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-$(command -v python3 || command -v python || true)}"
if [ -z "$PY" ]; then
    echo "cloudlogs: no python3 on PATH; set PYTHON=/path/to/python" >&2
    exit 1
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

# Report every address the viewer will actually answer on.
echo "cloudlogs: http://localhost:${PORT}"
if [ "$HOST" != "127.0.0.1" ] && [ "$HOST" != "localhost" ]; then
    lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -n "${lan_ip:-}" ] && echo "cloudlogs: http://${lan_ip}:${PORT}"
    if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
        echo "cloudlogs: running under WSL -- from Windows use http://localhost:${PORT}"
        echo "           (if that fails, use the address above, or see PLAN.md)"
    fi
fi

exec "$PY" -m uvicorn cloudlogs.main:app --reload --host "$HOST" --port "$PORT"
