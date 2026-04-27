#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/lib.sh
. "$SCRIPT_DIR/lib.sh"

require_root
# Pull latest code into the source repo before doing anything else,
# so that lib.sh and all deploy scripts are up-to-date for this run.
if git -C "$(repo_root)" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$(repo_root)" pull --ff-only --quiet || true
fi
ensure_base_packages
ensure_supported_python
log "upgrading pyBulletin in $PYBULLETIN_APP_DIR"
ensure_group
ensure_user
ensure_dialout_membership
ensure_audio_membership
ensure_rf_runtime_packages
ensure_layout
backup_config_if_present
sync_tree
ensure_selinux_contexts
install_config_if_missing
install_optional_config_if_missing "config/strings.toml" "strings.toml"
install_optional_config_if_missing "config/pybulletin.local.toml.example" "pybulletin.local.toml.example"
install_or_refresh_service
ensure_fail2ban_packages
install_or_refresh_fail2ban
install_or_refresh_logrotate
enable_service
restart_service_hard
enable_fail2ban_service
apply_imported_fail2ban_badips
wait_for_systemd_active "$PYBULLETIN_SERVICE_NAME" 45 || die "service failed to restart"
log "upgrade complete"
