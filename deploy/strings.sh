#!/usr/bin/env bash
# Push strings.toml to a running node without restarting the service.
# The StringCatalog hot-reloads within 30 seconds of the file changing.
#
# Usage:  bash deploy/strings.sh [user@]host
#         bash deploy/strings.sh root@pybulletin.ai3i.net
set -euo pipefail

HOST="${1:-}"
if [[ -z "$HOST" ]]; then
    echo "Usage: $0 [user@]host" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$SCRIPT_DIR/../config/strings.toml"
DEST="/home/pybulletin/pyBulletin/config/strings.toml"

rsync -v --checksum "$SRC" "${HOST}:${DEST}"
echo "[strings] pushed — hot-reload in ≤30s, no restart needed"
