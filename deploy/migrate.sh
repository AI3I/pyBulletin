#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/lib.sh
. "$SCRIPT_DIR/lib.sh"

usage() {
  cat <<'EOF'
Usage:
  sudo ./deploy/migrate.sh --from-fbb /fbb [--config /path/to/pybulletin.toml] [--dry-run]

Imports an existing LinFBB/FBB data directory into pyBulletin:
  - mail/  (message files)
  - users  (user database)
  - forward/ (routing table and neighbor definitions)
  - white pages database

Options:
  --from-fbb PATH   Path to the FBB data directory (required)
  --config PATH     Path to pybulletin.toml (default: installed location)
  --dry-run         Show what would be imported without writing anything
EOF
}

FBB_SOURCE=""
CONFIG_PATH=""
DRY_RUN=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --from-fbb|--source) FBB_SOURCE="${2:-}"; shift 2 ;;
    --config)            CONFIG_PATH="${2:-}"; shift 2 ;;
    --dry-run)           DRY_RUN=1; shift ;;
    -h|--help)           usage; exit 0 ;;
    *)                   die "unknown argument: $1" ;;
  esac
done

[ -n "$FBB_SOURCE" ] || { usage; die "--from-fbb is required"; }

require_root
ensure_base_packages
ensure_supported_python

ROOT="$(repo_root)"
[ -z "$CONFIG_PATH" ] && CONFIG_PATH="$PYBULLETIN_CONFIG_DEST"

CMD=(
  "$PYBULLETIN_PYTHON_LINK"
  "$ROOT/scripts/migrate_fbb.py"
  --config "$CONFIG_PATH"
  --source "$FBB_SOURCE"
)
[ "$DRY_RUN" = "1" ] && CMD+=(--dry-run)

log "migrating FBB data from $FBB_SOURCE"
(cd "$ROOT"; PYTHONPATH="$ROOT/src" "${CMD[@]}")
[ "$DRY_RUN" != "1" ] && apply_imported_fail2ban_badips
log "migration complete"
