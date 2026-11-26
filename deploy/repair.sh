#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/lib.sh
. "$SCRIPT_DIR/lib.sh"

require_root
PYBULLETIN_ENABLE_WEB=1
if [ -e "$PYBULLETIN_SYSTEMD_DIR/$PYBULLETIN_WEB_SERVICE_NAME" ]; then
  systemctl is-enabled --quiet "$PYBULLETIN_WEB_SERVICE_NAME" || PYBULLETIN_ENABLE_WEB=0
fi
ensure_base_packages
ensure_supported_python
log "repairing pyBulletin deployment in $PYBULLETIN_APP_DIR"
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
install_sysop_control_sudoers
ensure_fail2ban_packages
install_or_refresh_fail2ban
install_or_refresh_logrotate
enable_service
bootstrap_sysop_account
show_sysop_bootstrap_note
restart_service_hard
[ "$PYBULLETIN_ENABLE_WEB" = "0" ] || restart_web_service_hard
enable_fail2ban_service
apply_imported_fail2ban_badips
wait_for_systemd_active "$PYBULLETIN_SERVICE_NAME"     45 || die "core service failed to recover"
[ "$PYBULLETIN_ENABLE_WEB" = "0" ] || wait_for_systemd_active "$PYBULLETIN_WEB_SERVICE_NAME" 45 || die "web service failed to recover"
log "repair complete"
