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

audio_ok="no"
id -nG "$PYBULLETIN_USER" 2>/dev/null | grep -qE '\baudio\b' && audio_ok="yes"

# --- Services ---
for svc_var in SERVICE_NAME FORWARD_TIMER_NAME RETENTION_TIMER_NAME; do
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

db_path="" files_path="" transport="" tnc_device="" afsk_input="" afsk_output="" afsk_ptt=""
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
print(cfg.get("kiss", {}).get("transport", "disabled"))
print(cfg.get("kiss", {}).get("device", ""))
print(cfg.get("afsk", {}).get("input_device", ""))
print(cfg.get("afsk", {}).get("output_device", ""))
print(cfg.get("afsk", {}).get("ptt_device", ""))
PY
)
  db_path="${cfg_values[0]:-}"
  files_path="${cfg_values[1]:-}"
  transport="${cfg_values[2]:-disabled}"
  tnc_device="${cfg_values[3]:-}"
  afsk_input="${cfg_values[4]:-}"
  afsk_output="${cfg_values[5]:-}"
  afsk_ptt="${cfg_values[6]:-}"
fi

for var in db_path files_path; do
  eval "v=\$$var"
  if [ -n "$v" ] && [ "${v#/}" = "$v" ]; then
    eval "$var=$PYBULLETIN_APP_DIR/${v#./}"
  fi
done

db_ok="no";    [ -f "$db_path"    ] && db_ok="yes"
files_ok="no"; [ -d "$files_path" ] && files_ok="yes"

tnc_ok="not configured"
if [ -n "$tnc_device" ]; then
  [ -e "$tnc_device" ] && tnc_ok="present" || tnc_ok="missing ($tnc_device)"
fi

# --- AFSK / soundcard / PTT ---
sounddevice_ok="unknown"
pyaudio_ok="unknown"
audio_input_ok="not configured"
audio_output_ok="not configured"
ptt_ok="not configured"
cmedia_rule_ok="missing"
hidraw_found="none"

if [ -f /etc/udev/rules.d/99-pybulletin-cmedia.rules ] || [ -f /lib/udev/rules.d/99-pybulletin-cmedia.rules ]; then
  cmedia_rule_ok="present"
fi

if [ -f "$PYBULLETIN_CONFIG_DEST" ]; then
  sounddevice_ok="$("${PYBULLETIN_PYTHON_LINK:-/usr/bin/python3}" - <<PY
try:
    import sounddevice  # type: ignore
except Exception:
    print("missing")
else:
    print("available")
PY
)"
  pyaudio_ok="$("${PYBULLETIN_PYTHON_LINK:-/usr/bin/python3}" - <<PY
try:
    import pyaudio  # type: ignore
except Exception:
    print("missing")
else:
    print("available")
PY
)"
fi

[ -n "$afsk_input" ] && audio_input_ok="configured ($afsk_input)" || audio_input_ok="default"
[ -n "$afsk_output" ] && audio_output_ok="configured ($afsk_output)" || audio_output_ok="default"

case "$afsk_ptt" in
  "")
    ptt_ok="none"
    ;;
  serial_rts:*)
    ptt_dev="${afsk_ptt#serial_rts:}"
    [ -e "$ptt_dev" ] && ptt_ok="serial present ($ptt_dev)" || ptt_ok="serial missing ($ptt_dev)"
    ;;
  gpio:*)
    ptt_ok="bcm gpio (${afsk_ptt#gpio:})"
    ;;
  gpiochip:*)
    ptt_rest="${afsk_ptt#gpiochip:}"
    ptt_chip="${ptt_rest%:*}"
    [ -e "$ptt_chip" ] && ptt_ok="gpiochip present ($ptt_rest)" || ptt_ok="gpiochip missing ($ptt_rest)"
    ;;
  cm108:*)
    ptt_rest="${afsk_ptt#cm108:}"
    ptt_hid="${ptt_rest%:*}"
    [ -e "$ptt_hid" ] && ptt_ok="cm108 hidraw present ($ptt_rest)" || ptt_ok="cm108 hidraw missing ($ptt_rest)"
    ;;
  *)
    ptt_ok="unknown selector ($afsk_ptt)"
    ;;
esac

for dev in /sys/class/hidraw/hidraw*/device/uevent; do
  [ -f "$dev" ] || continue
  if grep -qi '^HID_ID=.*:00000d8c:' "$dev"; then
    hidraw_found="/dev/$(basename "$(dirname "$dev")")"
    break
  fi
done

# --- Kernel AX.25 is optional; pyBulletin uses userspace KISS/AFSK paths. ---
kernel_ax25="unavailable (not required)"
if lsmod 2>/dev/null | grep -q '^ax25 '; then
  kernel_ax25="loaded (not required)"
elif command -v modinfo >/dev/null 2>&1 && modinfo ax25 >/dev/null 2>&1; then
  kernel_ax25="available, not loaded (not required)"
fi

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
status "audio group"       "audio ($audio_ok)"
status "app dir"           "$PYBULLETIN_APP_DIR"
status "config"            "$PYBULLETIN_CONFIG_DEST ($config_ok)"
status "database"          "${db_path:-unset} ($db_ok)"
status "files dir"         "${files_path:-unset} ($files_ok)"
status "service"           "$PYBULLETIN_SERVICE_NAME ($svc_service_name)"
status "forward timer"     "$PYBULLETIN_FORWARD_TIMER_NAME ($svc_forward_timer_name)"
status "retention timer"   "$PYBULLETIN_RETENTION_TIMER_NAME ($svc_retention_timer_name)"
status "fail2ban"          "fail2ban.service ($fail2ban_state)"
status "selinux"           "$selinux_state"
status "kernel ax25"       "$kernel_ax25"
status "ax25 transport"    "${transport:-disabled}"
status "kiss tnc device"   "$tnc_ok"
status "afsk sounddevice"  "$sounddevice_ok"
status "afsk pyaudio"      "$pyaudio_ok"
status "afsk input"        "$audio_input_ok"
status "afsk output"       "$audio_output_ok"
status "afsk ptt"          "$ptt_ok"
status "cm108 udev rule"   "$cmedia_rule_ok"
status "cm108 hidraw"      "$hidraw_found"
status "api health"        "$api_health"
status "sysop bootstrap"   "$PYBULLETIN_SYSOP_BOOTSTRAP_NOTE ($sysop_bootstrap)"
