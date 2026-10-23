#!/usr/bin/env bash
# Writes CLIENT_ID=<client_id> into .env so start_client.sh picks it up.
#
# Usage:
#   ./set_client_id.sh <client_id>
#
# Example:
#   ./set_client_id.sh bed-3-room-2
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "usage: $0 <client_id>" >&2
    exit 1
fi

CLIENT_ID="$1"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

touch "$ENV_FILE"

if grep -q '^CLIENT_ID=' "$ENV_FILE" 2>/dev/null; then
    sed -i "s/^CLIENT_ID=.*/CLIENT_ID=${CLIENT_ID}/" "$ENV_FILE"
else
    echo "CLIENT_ID=${CLIENT_ID}" >> "$ENV_FILE"
fi

echo "CLIENT_ID set to ${CLIENT_ID} in $ENV_FILE"
