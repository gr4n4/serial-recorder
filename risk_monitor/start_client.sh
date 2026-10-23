#!/usr/bin/env bash
# Starts client/main.py in the background and detaches it from the
# terminal. Logs to logs/client.log, PID recorded in run/client.pid so
# stop_client.sh (or `kill $(cat run/client.pid)`) can stop it later.
#
# Usage:
#   ./start_client.sh [args passed through to python -m client.main]
#
# Examples:
#   ./start_client.sh --dry-run --server-url http://localhost:5000
#   ./start_client.sh --port /dev/ttyUSB0 --server-url http://localhost:5000
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="$SCRIPT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi
SERVER_IP="${SERVER_IP:-localhost}"
CLIENT_ID="${CLIENT_ID:-}"

RUN_DIR="$SCRIPT_DIR/run"
LOG_DIR="$SCRIPT_DIR/logs"
PID_FILE="$RUN_DIR/client.pid"
LOG_FILE="$LOG_DIR/client.log"

mkdir -p "$RUN_DIR" "$LOG_DIR"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "client already running (PID $(cat "$PID_FILE"))" >&2
    exit 1
fi

VENV_DIR="$SCRIPT_DIR/venv"
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "venv not found at $VENV_DIR -- creating it" >&2
    python3 -m venv "$VENV_DIR"
    source "$VENV_DIR/bin/activate"
    pip install -r "$SCRIPT_DIR/client/requirements.txt"
else
    source "$VENV_DIR/bin/activate"
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"

CLIENT_ID_ARGS=()
if [ -n "$CLIENT_ID" ]; then
    CLIENT_ID_ARGS=(--client-id "$CLIENT_ID")
fi

# $@ comes last so an explicit --client-id passed on the command line
# overrides the .env value (argparse keeps the last occurrence of a flag).
nohup "$PYTHON_BIN" -m client.main --server-url "http://${SERVER_IP}:5000" "${CLIENT_ID_ARGS[@]}" "$@" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
disown

echo "client started in background (PID $(cat "$PID_FILE")), logging to $LOG_FILE"
