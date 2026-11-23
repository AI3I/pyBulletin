#!/usr/bin/env bash
# Disposable, TCP-only LinBPQ node for local forwarding experiments.
set -euo pipefail

lab_dir="${LINBPQ_LAB_DIR:-/tmp/pybulletin-linbpq-lab}"
action="${1:-setup}"

if [[ "$action" == setup ]]; then
    mkdir -p "$lab_dir"
    if [[ ! -x "$lab_dir/linbpq64" ]]; then
        curl -fsSL 'https://www.cantab.net/users/john.wiseman/Downloads/linbpq64' -o "$lab_dir/linbpq64"
        chmod 700 "$lab_dir/linbpq64"
    fi
    if [[ ! -d "$lab_dir/HTML" ]]; then
        curl -fsSL 'https://www.cantab.net/users/john.wiseman/Downloads/HTMLPages.zip' -o "$lab_dir/HTMLPages.zip"
        unzip -q "$lab_dir/HTMLPages.zip" -d "$lab_dir/HTML"
    fi
    if [[ ! -f "$lab_dir/bpq32.cfg" ]]; then
        cat > "$lab_dir/bpq32.cfg" <<'CFG'
SIMPLE
LOCATOR=FN20
NODECALL=N0CALL-7
NODEALIAS=BPQLAB
BBS=1
NODE=1
PACLEN=236
IDINTERVAL=0
BTINTERVAL=0
NODESINTERVAL=0

PORT
 PORTNUM=1
 ID=Loopback Telnet
 DRIVER=Telnet
 QUALITY=0
 CONFIG
 LOGGING=0
 DisconnectOnClose=1
 SECURETELNET=1
 TCPPORT=18010
 FBBPORT=18011
 HTTPPORT=18080
 IPV4=1
 LOGINPROMPT=user:
 PASSWORDPROMPT=password:
 MAXSESSIONS=8
 USER=lab,labpass,N1CALL,BBS,SYSOP
ENDPORT

APPLICATION 1,BBS,,N0CALL-11,BPQLAB,255
LINMAIL
CFG
    fi
    printf 'LinBPQ lab prepared in %s\nRun: %s run\n' "$lab_dir" "$0"
elif [[ "$action" == run ]]; then
    if [[ ! -x "$lab_dir/linbpq64" || ! -f "$lab_dir/bpq32.cfg" ]]; then
        printf 'Run %s setup first.\n' "$0" >&2
        exit 1
    fi
    exec unshare -Urn -- sh -c 'ip link set lo up && cd "$1" && exec ./linbpq64 mail' sh "$lab_dir"
elif [[ "$action" == shell ]]; then
    if [[ ! -x "$lab_dir/linbpq64" || ! -f "$lab_dir/bpq32.cfg" ]]; then
        printf 'Run %s setup first.\n' "$0" >&2
        exit 1
    fi
    exec unshare -Urn -- bash -c '
        set -e
        ip link set lo up
        cd "$1"
        ./linbpq64 mail &
        bpq_pid=$!
        trap '\''kill "$bpq_pid" 2>/dev/null || true'\'' EXIT
        printf "LinBPQ is running inside this private shell. Use 127.0.0.1:18080 for its web UI.\n"
        bash
    ' bash "$lab_dir"
else
    printf 'Usage: %s [setup|run|shell]\n' "$0" >&2
    exit 2
fi
