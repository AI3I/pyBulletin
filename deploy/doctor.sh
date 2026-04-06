#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/lib.sh
. "$SCRIPT_DIR/lib.sh"

status() { printf '%-28s %s\n' "$1" "$2"; }

# --- OS / user ---
selinux_state="unavailable"
command -v getenforce >/dev/null 2>&1 && selinux_state="$(getenforce 2>/dev/null || printf 'unknown')"

app_user_ok="no"
id -u "$PYBULLETIN_USER" >/dev/null 2>&1 && app_user_ok="yes"

dialout_ok="no"
id -nG "$PYBULLETIN_USER" 2>/dev/null | grep -qE '\b(dialout|uucp|lock)\b' && dialout_ok="yes"

# --- Services ---
for svc_var in SERVICE_NAME WEB_SERVICE_NAME FORWARD_TIMER_NAME RETENTION_TIMER_NAME; do
  eval "unit=\$PYBULLETIN_${svc_var}"
  eval "state_var=svc_${svc_var,,}"
  s="missing"
  systemctl list-unit-files "$unit" >/dev/null 2>&1 && s="$(systemctl is-active "$unit" 2>/dev/null || true)"
  [ -n "$s" ] || s="inactive"
  eval "${state_var}='$s'"
done

fail2ban_state="missing"
systemctl list-unit-files fail2ban.service >/dev/null 2>&1 \
  && fail2ban_state="$(systemctl is-active fail2ban.service 2>/dev/null || true)"
[ -n "$fail2ban_state" ] || fail2ban_state="inactive"

# --- Config / data ---
config_ok="no"
[ -f "$PYBULLETIN_CONFIG_DEST" ] && config_ok="yes"

db_path="" files_path=""
if [ -f "$PYBULLETIN_CONFIG_DEST" ]; then
  readarray -t cfg_values < <("${PYBULLETIN_PYTHON_LINK:-/usr/bin/python3}" - <<PY
import sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib
from pathlib import Path
cfg = tomllib.loads(Path("$PYBULLETIN_CONFIG_DEST").read_text())
print(cfg.get("store", {}).get("sqlite_path", ""))
print(cfg.get("store", {}).get("files_path", ""))
PY
)
  db_path="${cfg_values[0]:-}"
  files_path="${cfg_values[1]:-}"
fi

for var in db_path files_path; do
  eval "v=\$$var"
  if [ -n "$v" ] && [ "${v#/}" = "$v" ]; then
    eval "$var=$PYBULLETIN_APP_DIR/${v#./}"
  fi
done

db_ok="no";    [ -f "$db_path"    ] && db_ok="yes"
files_ok="no"; [ -d "$files_path" ] && files_ok="yes"

# --- Serial TNC ---
tnc_device=""
if [ -f "$PYBULLETIN_CONFIG_DEST" ]; then
  tnc_device="$("${PYBULLETIN_PYTHON_LINK:-/usr/bin/python3}" - <<PY
import sys
try:
    import tomllib
except ImportError:
    import tomli as tomllib
from pathlib import Path
cfg = tomllib.loads(Path("$PYBULLETIN_CONFIG_DEST").read_text())
print(cfg.get("kiss", {}).get("device", ""))
PY
)"
fi
tnc_ok="not configured"
if [ -n "$tnc_device" ]; then
  [ -e "$tnc_device" ] && tnc_ok="present" || tnc_ok="missing ($tnc_device)"
fi

# --- AX.25 kernel module ---
ax25_mod="not loaded"
lsmod 2>/dev/null | grep -q '^ax25 ' && ax25_mod="loaded"

# --- API health ---
api_health="unavailable"
s="$(systemctl is-active "$PYBULLETIN_SERVICE_NAME" 2>/dev/null || true)"
if [ "$s" = "active" ]; then
  api_health="$(curl -fsS http://127.0.0.1:8080/api/health 2>/dev/null || printf 'unavailable')"
fi

sysop_bootstrap="no"
[ -f "$PYBULLETIN_SYSOP_BOOTSTRAP_NOTE" ] && sysop_bootstrap="yes"

# --- Print report ---
status "user"              "$PYBULLETIN_USER ($app_user_ok)"
status "serial group"      "dialout/lock ($dialout_ok)"
status "app dir"           "$PYBULLETIN_APP_DIR"
status "config"            "$PYBULLETIN_CONFIG_DEST ($config_ok)"
status "database"          "${db_path:-unset} ($db_ok)"
status "files dir"         "${files_path:-unset} ($files_ok)"
status "core service"      "$PYBULLETIN_SERVICE_NAME ($svc_service_name)"
status "web service"       "$PYBULLETIN_WEB_SERVICE_NAME ($svc_web_service_name)"
status "forward timer"     "$PYBULLETIN_FORWARD_TIMER_NAME ($svc_forward_timer_name)"
status "retention timer"   "$PYBULLETIN_RETENTION_TIMER_NAME ($svc_retention_timer_name)"
status "fail2ban"          "fail2ban.service ($fail2ban_state)"
status "selinux"           "$selinux_state"
status "ax25 module"       "$ax25_mod"
status "kiss tnc device"   "$tnc_ok"
status "api health"        "$api_health"
status "sysop bootstrap"   "$PYBULLETIN_SYSOP_BOOTSTRAP_NOTE ($sysop_bootstrap)"
