#!/usr/bin/env bash
# deploy/lib.sh — shared functions for pyBulletin deploy scripts
# Source this file; do not execute directly.

PYBULLETIN_USER="${PYBULLETIN_USER:-pybulletin}"
PYBULLETIN_GROUP="${PYBULLETIN_GROUP:-$PYBULLETIN_USER}"
PYBULLETIN_HOME="${PYBULLETIN_HOME:-/home/$PYBULLETIN_USER}"
PYBULLETIN_APP_DIR="${PYBULLETIN_APP_DIR:-$PYBULLETIN_HOME/pyBulletin}"
PYBULLETIN_SERVICE_NAME="${PYBULLETIN_SERVICE_NAME:-pybulletin.service}"
PYBULLETIN_FORWARD_SERVICE_NAME="${PYBULLETIN_FORWARD_SERVICE_NAME:-pybulletin-forward.service}"
PYBULLETIN_FORWARD_TIMER_NAME="${PYBULLETIN_FORWARD_TIMER_NAME:-pybulletin-forward.timer}"
PYBULLETIN_RETENTION_SERVICE_NAME="${PYBULLETIN_RETENTION_SERVICE_NAME:-pybulletin-retention.service}"
PYBULLETIN_RETENTION_TIMER_NAME="${PYBULLETIN_RETENTION_TIMER_NAME:-pybulletin-retention.timer}"
PYBULLETIN_SYSTEMD_DIR="${PYBULLETIN_SYSTEMD_DIR:-/etc/systemd/system}"
PYBULLETIN_CONFIG_SRC="${PYBULLETIN_CONFIG_SRC:-config/pybulletin.toml}"
PYBULLETIN_CONFIG_DEST="${PYBULLETIN_CONFIG_DEST:-$PYBULLETIN_APP_DIR/config/pybulletin.toml}"
PYBULLETIN_PKG_AUTO_INSTALL="${PYBULLETIN_PKG_AUTO_INSTALL:-1}"
PYBULLETIN_PYTHON_LINK="${PYBULLETIN_PYTHON_LINK:-/usr/local/bin/pybulletin-python}"
PYBULLETIN_FAIL2BAN_DIR="${PYBULLETIN_FAIL2BAN_DIR:-/etc/fail2ban}"
PYBULLETIN_LOGROTATE_DIR="${PYBULLETIN_LOGROTATE_DIR:-/etc/logrotate.d}"
PYBULLETIN_UDEV_RULES_DIR="${PYBULLETIN_UDEV_RULES_DIR:-/etc/udev/rules.d}"
PYBULLETIN_FAIL2BAN_BADIP_LIST="${PYBULLETIN_FAIL2BAN_BADIP_LIST:-$PYBULLETIN_APP_DIR/config/fail2ban-badip.local}"
PYBULLETIN_FAIL2BAN_BADIP_STATE="${PYBULLETIN_FAIL2BAN_BADIP_STATE:-$PYBULLETIN_APP_DIR/data/fail2ban-badip-applied.txt}"
PYBULLETIN_SYSOP_BOOTSTRAP_NOTE="${PYBULLETIN_SYSOP_BOOTSTRAP_NOTE:-/root/pybulletin-initial-sysop.txt}"
PYBULLETIN_TMP_SWAPFILE="${PYBULLETIN_TMP_SWAPFILE:-/swapfile-pybulletin}"
PYBULLETIN_TMP_SWAP_MB="${PYBULLETIN_TMP_SWAP_MB:-1024}"
PYBULLETIN_CONFIG_BACKUP_DIR="${PYBULLETIN_CONFIG_BACKUP_DIR:-$PYBULLETIN_APP_DIR/config/backups}"

repo_root() {
  local src
  src="${BASH_SOURCE[0]}"
  while [ -h "$src" ]; do
    src="$(readlink "$src")"
  done
  cd "$(dirname "$src")/.." && pwd
}

log() {
  printf '[pybulletin] %s\n' "$*"
}

die() {
  printf '[pybulletin] ERROR: %s\n' "$*" >&2
  exit 1
}

os_release_value() {
  local key="$1"
  [ -r /etc/os-release ] || return 1
  awk -F= -v key="$key" '$1 == key {gsub(/^"/, "", $2); gsub(/"$/, "", $2); print $2}' /etc/os-release
}

os_id()   { os_release_value ID; }
os_like() { os_release_value ID_LIKE; }

pkg_manager() {
  if command -v apt-get >/dev/null 2>&1; then printf 'apt'; return; fi
  if command -v dnf     >/dev/null 2>&1; then printf 'dnf'; return; fi
  if command -v yum     >/dev/null 2>&1; then printf 'yum'; return; fi
  return 1
}

mem_total_mb() {
  awk '/MemTotal:/ {printf "%d\n", $2 / 1024}' /proc/meminfo 2>/dev/null || printf '0\n'
}

swap_total_mb() {
  awk '/SwapTotal:/ {printf "%d\n", $2 / 1024}' /proc/meminfo 2>/dev/null || printf '0\n'
}

maybe_enable_temp_swap() {
  local mem_mb swap_mb
  mem_mb="$(mem_total_mb)"
  swap_mb="$(swap_total_mb)"
  [ "${mem_mb:-0}" -ge 1400 ] || [ "${swap_mb:-0}" -gt 0 ] && return 0
  [ -e "$PYBULLETIN_TMP_SWAPFILE" ] && return 0
  command -v swapon >/dev/null 2>&1 && command -v mkswap >/dev/null 2>&1 || return 0
  log "enabling temporary swap (${PYBULLETIN_TMP_SWAP_MB}MB) for package installation"
  if command -v fallocate >/dev/null 2>&1; then
    fallocate -l "${PYBULLETIN_TMP_SWAP_MB}M" "$PYBULLETIN_TMP_SWAPFILE"
  else
    dd if=/dev/zero of="$PYBULLETIN_TMP_SWAPFILE" bs=1M count="$PYBULLETIN_TMP_SWAP_MB" status=none
  fi
  chmod 600 "$PYBULLETIN_TMP_SWAPFILE"
  mkswap "$PYBULLETIN_TMP_SWAPFILE" >/dev/null
  swapon "$PYBULLETIN_TMP_SWAPFILE"
}

disable_temp_swap() {
  if [ -e "$PYBULLETIN_TMP_SWAPFILE" ]; then
    swapoff "$PYBULLETIN_TMP_SWAPFILE" >/dev/null 2>&1 || true
    rm -f "$PYBULLETIN_TMP_SWAPFILE"
  fi
}

install_packages() {
  [ "$PYBULLETIN_PKG_AUTO_INSTALL" = "1" ] || return 0
  [ "$#" -gt 0 ] || return 0
  local mgr
  mgr="$(pkg_manager)" || die "no supported package manager found"
  case "$mgr" in
    apt)
      # Skip apt-get entirely if every requested package is already installed.
      local missing=()
      for pkg in "$@"; do
        dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing+=("$pkg")
      done
      [ "${#missing[@]}" -eq 0 ] && return 0
      export DEBIAN_FRONTEND=noninteractive
      apt-get update
      apt-get install -y "${missing[@]}"
      ;;
    dnf)
      maybe_enable_temp_swap
      dnf install -y --setopt=install_weak_deps=False "$@" || { disable_temp_swap; return 1; }
      disable_temp_swap
      ;;
    yum)
      maybe_enable_temp_swap
      yum install -y "$@" || { disable_temp_swap; return 1; }
      disable_temp_swap
      ;;
  esac
}

ensure_base_packages() {
  local mgr
  mgr="$(pkg_manager)" || die "no supported package manager found"
  case "$mgr" in
    apt)  install_packages rsync python3 ca-certificates curl git ;;
    dnf|yum) install_packages rsync python3 ca-certificates curl git policycoreutils ;;
  esac
}

python_version_ok() {
  local bin="$1"
  [ -x "$bin" ] || return 1
  "$bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

selected_python_bin() {
  for bin in \
    /usr/bin/python3.13 /usr/bin/python3.12 /usr/bin/python3.11 /usr/bin/python3.10 \
    /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
    /usr/local/bin/python3.11 /usr/local/bin/python3.10 \
    /usr/bin/python3
  do
    python_version_ok "$bin" && printf '%s' "$bin" && return 0
  done
  return 1
}

ensure_supported_python() {
  local mgr current
  if current="$(selected_python_bin)"; then
    ln -sf "$current" "$PYBULLETIN_PYTHON_LINK"
    return 0
  fi
  mgr="$(pkg_manager)" || die "no supported package manager found"
  case "$mgr" in
    apt) die "Python 3.10+ is required; install a newer Python runtime" ;;
    dnf|yum) install_packages python3.12 || install_packages python3.11 || install_packages python3.10 ;;
  esac
  current="$(selected_python_bin)" || die "unable to locate Python 3.10+ after package install"
  ln -sf "$current" "$PYBULLETIN_PYTHON_LINK"
}

require_root() {
  [ "$(id -u)" -eq 0 ] || die "run as root"
}

ensure_group() {
  getent group "$PYBULLETIN_GROUP" >/dev/null || groupadd --system "$PYBULLETIN_GROUP"
}

ensure_user() {
  if ! id -u "$PYBULLETIN_USER" >/dev/null 2>&1; then
    useradd \
      --system \
      --create-home \
      --home-dir "$PYBULLETIN_HOME" \
      --gid "$PYBULLETIN_GROUP" \
      --shell /bin/bash \
      "$PYBULLETIN_USER"
  fi
}

ensure_dialout_membership() {
  # Grant serial TNC access — group name varies by distro
  local mgr serial_group
  mgr="$(pkg_manager)" || return 0
  case "$mgr" in
    apt)     serial_group="dialout" ;;
    dnf|yum) serial_group="dialout" ;;
    *)       serial_group="dialout" ;;
  esac
  if getent group "$serial_group" >/dev/null 2>&1; then
    usermod -aG "$serial_group" "$PYBULLETIN_USER" || true
    log "added $PYBULLETIN_USER to group $serial_group for serial TNC access"
  fi
  # Also add to 'lock' group (Fedora/RHEL) if present
  if getent group lock >/dev/null 2>&1; then
    usermod -aG lock "$PYBULLETIN_USER" || true
  fi
}

ensure_audio_membership() {
  if getent group audio >/dev/null 2>&1; then
    usermod -aG audio "$PYBULLETIN_USER" || true
    log "added $PYBULLETIN_USER to group audio for USB soundcard and hidraw access"
  fi
}

install_optional_packages() {
  [ "$PYBULLETIN_PKG_AUTO_INSTALL" = "1" ] || return 0
  [ "$#" -gt 0 ] || return 0
  local pkg
  for pkg in "$@"; do
    install_packages "$pkg" || log "optional package not installed: $pkg"
  done
}

ensure_rf_runtime_packages() {
  local mgr
  mgr="$(pkg_manager)" || return 0
  case "$mgr" in
    apt)
      install_optional_packages \
        libportaudio2 \
        python3-pyaudio \
        python3-serial \
        python3-libgpiod \
        alsa-utils
      ;;
    dnf|yum)
      install_optional_packages \
        portaudio \
        python3-pyaudio \
        python3-pyserial \
        python3-libgpiod \
        alsa-utils
      ;;
  esac
}

ensure_layout() {
  install -d -o "$PYBULLETIN_USER" -g "$PYBULLETIN_GROUP"       "$PYBULLETIN_APP_DIR"
  install -d -o "$PYBULLETIN_USER" -g "$PYBULLETIN_GROUP"       "$PYBULLETIN_APP_DIR/data"
  install -d -o "$PYBULLETIN_USER" -g "$PYBULLETIN_GROUP"       "$PYBULLETIN_APP_DIR/data/files"
  install -d -o "$PYBULLETIN_USER" -g "$PYBULLETIN_GROUP"       "$PYBULLETIN_APP_DIR/logs"
  install -d -o "$PYBULLETIN_USER" -g "$PYBULLETIN_GROUP"       "$PYBULLETIN_APP_DIR/config"
  install -d -o "$PYBULLETIN_USER" -g "$PYBULLETIN_GROUP" -m 0750 /var/log/pybulletin
  touch /var/log/pybulletin/authfail.log
  chown "$PYBULLETIN_USER:$PYBULLETIN_GROUP" /var/log/pybulletin/authfail.log
  chmod 0640 /var/log/pybulletin/authfail.log
}

ensure_runtime_ownership() {
  [ -d "$PYBULLETIN_APP_DIR" ] && chown -R "$PYBULLETIN_USER:$PYBULLETIN_GROUP" "$PYBULLETIN_APP_DIR"
}

ensure_selinux_contexts() {
  if command -v restorecon >/dev/null 2>&1; then
    restorecon -RF "$PYBULLETIN_HOME" >/dev/null 2>&1 || true
    # Serial devices — restore context so the service user can open them
    for dev in /dev/ttyS* /dev/ttyUSB* /dev/ttyACM*; do
      [ -e "$dev" ] && restorecon "$dev" >/dev/null 2>&1 || true
    done
    log "SELinux contexts restored"
  fi
}

sync_tree() {
  local root
  root="$(repo_root)"
  # Sync everything except node-specific config, data, and logs.
  # strings.toml is part of the software and IS updated on every upgrade.
  # pybulletin.toml (node config) is protected by install_config_if_missing.
  rsync -a \
    --delete \
    --exclude '.git/' \
    --exclude '.pytest_cache/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude 'config/pybulletin.toml' \
    --exclude 'config/pybulletin.local.toml' \
    --exclude 'data/' \
    --exclude 'logs/' \
    "$root"/ "$PYBULLETIN_APP_DIR"/
  chown -R "$PYBULLETIN_USER:$PYBULLETIN_GROUP" "$PYBULLETIN_APP_DIR"
}

install_config_if_missing() {
  local root
  root="$(repo_root)"
  if [ ! -f "$PYBULLETIN_CONFIG_DEST" ]; then
    install -o "$PYBULLETIN_USER" -g "$PYBULLETIN_GROUP" -m 0640 \
      "$root/$PYBULLETIN_CONFIG_SRC" "$PYBULLETIN_CONFIG_DEST"
  fi
}

backup_config_if_present() {
  local stamp dest_dir config_dir file base
  config_dir="$(dirname "$PYBULLETIN_CONFIG_DEST")"
  [ -d "$config_dir" ] || return 0
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  dest_dir="$PYBULLETIN_CONFIG_BACKUP_DIR/$stamp"
  for file in "$PYBULLETIN_CONFIG_DEST" "$config_dir/pybulletin.local.toml"; do
    [ -f "$file" ] || continue
    install -d -o "$PYBULLETIN_USER" -g "$PYBULLETIN_GROUP" -m 0750 "$dest_dir"
    base="$(basename "$file")"
    install -o "$PYBULLETIN_USER" -g "$PYBULLETIN_GROUP" -m 0640 "$file" "$dest_dir/$base"
  done
  [ -d "$dest_dir" ] && log "config backup created at $dest_dir"
}

install_optional_config_if_missing() {
  local relative_src="$1" dest_name="$2" root dest
  root="$(repo_root)"
  dest="$(dirname "$PYBULLETIN_CONFIG_DEST")/$dest_name"
  if [ ! -f "$dest" ] && [ -f "$root/$relative_src" ]; then
    install -o "$PYBULLETIN_USER" -g "$PYBULLETIN_GROUP" -m 0640 \
      "$root/$relative_src" "$dest"
  fi
}

install_or_refresh_service() {
  local root
  root="$(repo_root)"
  for unit in \
    pybulletin.service \
    pybulletin-forward.service \
    pybulletin-forward.timer \
    pybulletin-retention.service \
    pybulletin-retention.timer
  do
    install -o root -g root -m 0644 \
      "$root/deploy/systemd/$unit" \
      "$PYBULLETIN_SYSTEMD_DIR/$unit"
  done
  # Remove legacy split web service; pybulletin.service now serves core + web.
  systemctl disable --now pybulletin-web.service >/dev/null 2>&1 || true
  rm -f "$PYBULLETIN_SYSTEMD_DIR/pybulletin-web.service"
  systemctl daemon-reload
  systemctl reset-failed pybulletin-web.service >/dev/null 2>&1 || true
}

service_is_active()     { systemctl is-active --quiet "$PYBULLETIN_SERVICE_NAME"; }

wait_for_systemd_active() {
  local unit="$1" timeout="${2:-30}" start now state
  start="$(date +%s)"
  while true; do
    state="$(systemctl is-active "$unit" 2>/dev/null || true)"
    case "$state" in
      active) return 0 ;;
      failed|inactive|deactivating) return 1 ;;
    esac
    now="$(date +%s)"
    [ $((now - start)) -ge "$timeout" ] && return 1
    sleep 1
  done
}

restart_service_hard() {
  service_is_active && { systemctl kill -s SIGKILL "$PYBULLETIN_SERVICE_NAME" || true; sleep 1; }
  systemctl start "$PYBULLETIN_SERVICE_NAME"
}

enable_service() {
  systemctl enable "$PYBULLETIN_SERVICE_NAME" >/dev/null
  systemctl enable --now "$PYBULLETIN_FORWARD_TIMER_NAME"   >/dev/null
  systemctl enable --now "$PYBULLETIN_RETENTION_TIMER_NAME" >/dev/null
}

disable_service() {
  systemctl disable "$PYBULLETIN_SERVICE_NAME"     >/dev/null 2>&1 || true
  systemctl disable --now "$PYBULLETIN_FORWARD_TIMER_NAME"   >/dev/null 2>&1 || true
  systemctl disable --now "$PYBULLETIN_RETENTION_TIMER_NAME" >/dev/null 2>&1 || true
}

stop_service() {
  systemctl stop "$PYBULLETIN_SERVICE_NAME"        >/dev/null 2>&1 || true
  systemctl stop "$PYBULLETIN_FORWARD_TIMER_NAME"  >/dev/null 2>&1 || true
  systemctl stop "$PYBULLETIN_RETENTION_TIMER_NAME" >/dev/null 2>&1 || true
}

ensure_fail2ban_packages() {
  local mgr
  mgr="$(pkg_manager)" || die "no supported package manager found"
  case "$mgr" in
    apt) install_packages fail2ban ;;
    dnf|yum)
      [ "$(os_id)" != "fedora" ] && { install_packages epel-release || true; }
      install_packages fail2ban
      ;;
  esac
}

install_or_refresh_fail2ban() {
  local root
  root="$(repo_root)"
  install -d -m 0755 "$PYBULLETIN_FAIL2BAN_DIR/filter.d" "$PYBULLETIN_FAIL2BAN_DIR/jail.d"
  for f in \
    filter.d/pybulletin-auth-core.conf \
    filter.d/pybulletin-auth-web.conf \
    jail.d/pybulletin-core.local \
    jail.d/pybulletin-web.local
  do
    install -o root -g root -m 0644 \
      "$root/deploy/fail2ban/$f" \
      "$PYBULLETIN_FAIL2BAN_DIR/$f"
  done
  cat >"$PYBULLETIN_FAIL2BAN_DIR/jail.d/pybulletin-disable-defaults.local" <<'EOF'
[sshd]
enabled = false
EOF
}

install_or_refresh_logrotate() {
  local root
  root="$(repo_root)"
  install -d -m 0755 "$PYBULLETIN_LOGROTATE_DIR"
  install -o root -g root -m 0644 \
    "$root/deploy/logrotate/pybulletin" \
    "$PYBULLETIN_LOGROTATE_DIR/pybulletin"
}

install_or_refresh_udev_rules() {
  local root
  root="$(repo_root)"
  install -d -m 0755 "$PYBULLETIN_UDEV_RULES_DIR"
  install -o root -g root -m 0644 \
    "$root/deploy/udev/99-pybulletin-cmedia.rules" \
    "$PYBULLETIN_UDEV_RULES_DIR/99-pybulletin-cmedia.rules"
  if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules >/dev/null 2>&1 || true
    udevadm trigger --subsystem-match=hidraw >/dev/null 2>&1 || true
  fi
}

enable_fail2ban_service() {
  systemctl list-unit-files fail2ban.service >/dev/null 2>&1 || return 0
  systemctl enable fail2ban >/dev/null 2>&1 || true
  systemctl restart fail2ban
}

apply_imported_fail2ban_badips() {
  local client="/usr/bin/fail2ban-client"
  [ -x "$client" ] || return 0
  systemctl list-unit-files fail2ban.service >/dev/null 2>&1 || return 0
  install -d -o "$PYBULLETIN_USER" -g "$PYBULLETIN_GROUP" "$PYBULLETIN_APP_DIR/data"
  local tmp prev
  tmp="$(mktemp)"
  if [ -f "$PYBULLETIN_FAIL2BAN_BADIP_LIST" ]; then
    grep -v '^[[:space:]]*#' "$PYBULLETIN_FAIL2BAN_BADIP_LIST" | sed '/^[[:space:]]*$/d' | sort -u >"$tmp" || true
  else
    : >"$tmp"
  fi
  if [ -f "$PYBULLETIN_FAIL2BAN_BADIP_STATE" ]; then
    prev="$(mktemp)"
    sort -u "$PYBULLETIN_FAIL2BAN_BADIP_STATE" >"$prev"
  else
    prev="$(mktemp)"
    : >"$prev"
  fi
  for entry in $(comm -23 "$prev" "$tmp"); do
    for jail in pybulletin-core-auth pybulletin-web-auth; do
      "$client" set "$jail" unbanip "$entry" >/dev/null 2>&1 || true
    done
  done
  for entry in $(comm -13 "$prev" "$tmp"); do
    for jail in pybulletin-core-auth pybulletin-web-auth; do
      "$client" set "$jail" banip "$entry" >/dev/null 2>&1 || true
    done
  done
  install -o "$PYBULLETIN_USER" -g "$PYBULLETIN_GROUP" -m 0640 "$tmp" "$PYBULLETIN_FAIL2BAN_BADIP_STATE"
  rm -f "$tmp" "$prev"
}

bootstrap_sysop_account() {
  if (
    cd "$PYBULLETIN_APP_DIR" &&
    PYTHONPATH=src "$PYBULLETIN_PYTHON_LINK" scripts/bootstrap_sysop.py \
      --config "$PYBULLETIN_CONFIG_DEST" \
      --output "$PYBULLETIN_SYSOP_BOOTSTRAP_NOTE"
  ); then
    ensure_runtime_ownership
    chmod 0600 "$PYBULLETIN_SYSOP_BOOTSTRAP_NOTE" >/dev/null 2>&1 || true
    log "SYSOP bootstrap note at $PYBULLETIN_SYSOP_BOOTSTRAP_NOTE"
  else
    die "failed to seed SYSOP bootstrap account"
  fi
}

show_sysop_bootstrap_note() {
  [ -f "$PYBULLETIN_SYSOP_BOOTSTRAP_NOTE" ] || { log "SYSOP bootstrap note not found"; return; }
  printf '\n'
  printf '################################################################################\n'
  printf '#  READ THIS NOW: INITIAL SYSOP CREDENTIALS ARE PRINTED BELOW                 #\n'
  printf '#  SAVE THESE CREDENTIALS BEFORE YOU LEAVE THIS INSTALLER                     #\n'
  printf '################################################################################\n\n'
  printf '========================================================================\n'
  printf ' pyBulletin Initial System Operator Credentials\n'
  printf '========================================================================\n'
  cat "$PYBULLETIN_SYSOP_BOOTSTRAP_NOTE"
  printf '\nBackup file: %s\n' "$PYBULLETIN_SYSOP_BOOTSTRAP_NOTE"
  printf '========================================================================\n\n'
  if [ -t 0 ]; then
    local ack=""
    while [ "$ack" != "READ" ]; do
      printf 'Type READ after you have saved the bootstrap credentials: '
      IFS= read -r ack
    done
    printf '\n'
  fi
}
