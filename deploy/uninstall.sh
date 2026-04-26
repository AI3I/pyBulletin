#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/lib.sh
. "$SCRIPT_DIR/lib.sh"

require_root

KEEP_DATA="${KEEP_DATA:-1}"
KEEP_CONFIG="${KEEP_CONFIG:-1}"

log "uninstalling pyBulletin from $PYBULLETIN_APP_DIR"
disable_service
stop_service

for unit in \
  "$PYBULLETIN_SERVICE_NAME" \
  pybulletin-web.service \
  "$PYBULLETIN_FORWARD_SERVICE_NAME" \
  "$PYBULLETIN_FORWARD_TIMER_NAME" \
  "$PYBULLETIN_RETENTION_SERVICE_NAME" \
  "$PYBULLETIN_RETENTION_TIMER_NAME"
do
  rm -f "$PYBULLETIN_SYSTEMD_DIR/$unit"
done

rm -f "$PYBULLETIN_FAIL2BAN_DIR/filter.d/pybulletin-auth-core.conf"
rm -f "$PYBULLETIN_FAIL2BAN_DIR/filter.d/pybulletin-auth-web.conf"
rm -f "$PYBULLETIN_FAIL2BAN_DIR/jail.d/pybulletin-core.local"
rm -f "$PYBULLETIN_FAIL2BAN_DIR/jail.d/pybulletin-web.local"
rm -f "$PYBULLETIN_FAIL2BAN_DIR/jail.d/pybulletin-disable-defaults.local"
rm -f "$PYBULLETIN_LOGROTATE_DIR/pybulletin"
rm -f "$PYBULLETIN_SYSOP_BOOTSTRAP_NOTE"
rm -f "$PYBULLETIN_FAIL2BAN_BADIP_STATE"

systemctl daemon-reload
systemctl restart fail2ban >/dev/null 2>&1 || true

if [ -d "$PYBULLETIN_APP_DIR" ]; then
  if [ "$KEEP_DATA" = "1" ] || [ "$KEEP_CONFIG" = "1" ]; then
    tmp_keep="$(mktemp -d)"
    [ "$KEEP_DATA"   = "1" ] && [ -d "$PYBULLETIN_APP_DIR/data"   ] && mv "$PYBULLETIN_APP_DIR/data"   "$tmp_keep/data"
    [ "$KEEP_CONFIG" = "1" ] && [ -d "$PYBULLETIN_APP_DIR/config" ] && mv "$PYBULLETIN_APP_DIR/config" "$tmp_keep/config"
    rm -rf "$PYBULLETIN_APP_DIR"
    install -d -o "$PYBULLETIN_USER" -g "$PYBULLETIN_GROUP" "$PYBULLETIN_APP_DIR"
    [ -d "$tmp_keep/data"   ] && mv "$tmp_keep/data"   "$PYBULLETIN_APP_DIR/data"
    [ -d "$tmp_keep/config" ] && mv "$tmp_keep/config" "$PYBULLETIN_APP_DIR/config"
    chown -R "$PYBULLETIN_USER:$PYBULLETIN_GROUP" "$PYBULLETIN_APP_DIR"
    rmdir "$tmp_keep" 2>/dev/null || true
  else
    rm -rf "$PYBULLETIN_APP_DIR"
  fi
fi

log "uninstall complete (KEEP_DATA=$KEEP_DATA KEEP_CONFIG=$KEEP_CONFIG)"
