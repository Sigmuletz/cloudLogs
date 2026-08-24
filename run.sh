#!/usr/bin/env bash
# Start the cloudlogs viewer. Ingest runs automatically at startup when
# data/logs.json is missing or older than its inputs or than rules.yaml.
#
#   ./run.sh                          # http://localhost:8000, default input
#   ./run.sh path/to/app.log          # ingest this file instead
#   ./run.sh a.log b.log logs/        # several files, or a directory
#   ./run.sh 'logs/**/*.log'          # a glob -- quote it so the shell keeps it
#   PORT=9000 ./run.sh app.log
#   HOST=127.0.0.1 ./run.sh           # loopback only (see the note below)
#   CLOUDLOGS_INPUT='logs/**/*.log' ./run.sh    # same thing, as an environment
#
# Relative paths are taken from the directory you ran this in, not from the
# project root. Arguments win over CLOUDLOGS_INPUT.
#
# The default bind is 0.0.0.0 so the server is reachable from outside the
# machine it runs on -- from Windows when this is a WSL distro, and from other
# hosts on the LAN. There is no authentication, so on an untrusted network set
# HOST=127.0.0.1. Windows/LAN details: see "Running outside WSL" in PLAN.md.
set -euo pipefail

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
fi

# Resolve the inputs against the caller's directory before cd'ing away. A glob
# is kept as a pattern -- ingest expands it -- so this cannot use realpath.
launch_pwd="$PWD"
cd "$(dirname "$0")"

if [ "$#" -gt 0 ]; then
    inputs=""
    for arg in "$@"; do
        case "$arg" in
            /*) abs="$arg" ;;
            *)  abs="$launch_pwd/$arg" ;;
        esac
        inputs="${inputs:+$inputs:}$abs"
    done
    export CLOUDLOGS_INPUT="$inputs"
    echo "cloudlogs: input $CLOUDLOGS_INPUT"
elif [ -n "${CLOUDLOGS_INPUT:-}" ]; then
    echo "cloudlogs: input $CLOUDLOGS_INPUT  (from CLOUDLOGS_INPUT)"
fi

PY="${PYTHON:-$(command -v python3 || command -v python || true)}"
if [ -z "$PY" ]; then
    echo "cloudlogs: no python3 on PATH; set PYTHON=/path/to/python" >&2
    exit 1
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

# Report every address the viewer will actually answer on. Under WSL2 in its
# default NAT mode the distro's own IP is reachable from the Windows host but
# NOT from the LAN, so printing it as if it were a LAN address is a lie -- say
# what is actually needed instead.
echo "cloudlogs: http://localhost:${PORT}"
if [ "$HOST" != "127.0.0.1" ] && [ "$HOST" != "localhost" ]; then
    ips="$(hostname -I 2>/dev/null || true)"
    lan_ip="$(printf '%s\n' $ips | awk '{print $1}' | head -1)"
    is_wsl=false
    grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null && is_wsl=true

    # In mirrored mode the distro shares the Windows network stack, so it holds
    # a real LAN address rather than a 172.16/12 NAT one.
    routable_ip="$(printf '%s\n' $ips | tr ' ' '\n' \
        | grep -vE '^(172\.(1[6-9]|2[0-9]|3[01])\.|127\.|169\.254\.)' | head -1 || true)"

    if [ "$is_wsl" = false ]; then
        [ -n "${lan_ip:-}" ] && echo "cloudlogs: http://${lan_ip}:${PORT}  (LAN)"
    elif [ -n "${routable_ip:-}" ]; then
        echo "cloudlogs: http://${routable_ip}:${PORT}  (LAN -- WSL mirrored networking)"
    else
        echo "cloudlogs: from Windows use http://localhost:${PORT}"
        host_ip=""
        ps_exe=/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe
        if [ -x "$ps_exe" ]; then
            host_ip="$("$ps_exe" -NoProfile -Command \
                "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { \$_.IPAddress -notlike '127.*' -and \$_.IPAddress -notlike '172.*' -and \$_.IPAddress -notlike '169.254.*' } | Select-Object -First 1).IPAddress" \
                2>/dev/null | tr -d '\r\n' || true)"
        fi
        echo "cloudlogs: other machines on the LAN cannot reach this distro directly --"
        echo "           WSL2 NAT puts it behind ${lan_ip:-an internal address}."
        if [ -n "$host_ip" ]; then
            echo "           After ./share-lan.ps1 they reach it at http://${host_ip}:${PORT}"
        fi
        echo "           Run in an elevated PowerShell on Windows:  .\\share-lan.ps1 -Port ${PORT}"
        echo "           (or switch to mirrored networking -- see PLAN.md 5.1)"
    fi
fi

exec "$PY" -m uvicorn cloudlogs.main:app --reload --host "$HOST" --port "$PORT"
