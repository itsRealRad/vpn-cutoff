#!/usr/bin/env python3
"""Build a single-file, self-contained installer for xui-user-cutoff.

Everything (watcher, config, systemd unit, logrotate, access-log enabler) is
embedded as base64 so the result is one scp-able .sh with no side files and no
quoting hazards. Paths are auto-detected at install time.
"""
import base64, os

SRC = os.path.dirname(os.path.abspath(__file__))


def b64(name):
    with open(os.path.join(SRC, name), "rb") as fh:
        return base64.b64encode(fh.read()).decode()


WATCHER = b64("xui_user_cutoff.py")
ACCLOG = b64("set_accesslog.py")
LOGROT = b64("xray-access.logrotate")
VERSION = "0.0.1"

TEMPLATE = r'''#!/bin/bash
# xui-user-cutoff installer (self-contained) -- v@VERSION@
#
# Cuts the LIVE sessions of 3x-ui clients that are expired / over quota /
# disabled, without restarting xray. TCP sessions die instantly (ss -K on the
# exact 5-tuple read from xray's access log); UDP/QUIC (hysteria, tuic, masque)
# are blocked per-flow at the kernel; QUIC cannot be cut instantly because it
# ignores ICMP and does not connect() its UDP socket, so the client only
# notices on its own idle timeout.
#
# Usage:
#   ./install-xui-cutoff.sh                 # install / upgrade, no log change
#   ./install-xui-cutoff.sh --with-accesslog   # also enable xray's access log
#   ./install-xui-cutoff.sh --accesslog-only   # only enable the access log
#   ./install-xui-cutoff.sh --uninstall
#   ./install-xui-cutoff.sh --status
#
# The access log is REQUIRED for per-flow precision (telling two users behind
# one IP apart). Enabling it needs ONE xray restart, because xray binds its log
# path at startup and `xray api restartlogger` does not re-read the config.
set -euo pipefail

VERSION="@VERSION@"
PREFIX=/usr/local/bin
CONF=/etc/xui-user-cutoff.conf
UNIT=/etc/systemd/system/xui-user-cutoff.service
LOGROTATE=/etc/logrotate.d/xray-access
PY=$PREFIX/xui_user_cutoff.py
ACCLOG_TOOL=$PREFIX/xui-set-accesslog.py
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

say(){ printf '%s\n' "$*"; }
die(){ printf 'ERROR: %s\n' "$*" >&2; exit 1; }
warn(){ printf 'WARN: %s\n' "$*" >&2; }

[ "$(id -u)" = 0 ] || die "must run as root"

# ---------- prerequisites (installed automatically) ----------
# Everything this tool needs from the OS. Nothing here touches the 3x-ui panel
# itself -- that must already be installed. Each item is probed with the same
# command the tool will later use, so "present" means "actually works".
PKG_MGR=""; PKG_INSTALL=""; PKG_UPDATE=""
detect_pkg(){
  if command -v apt-get >/dev/null 2>&1; then
    PKG_MGR=apt; export DEBIAN_FRONTEND=noninteractive
    PKG_UPDATE="apt-get update -qq"
    PKG_INSTALL="apt-get install -y -qq --no-install-recommends"
  elif command -v dnf >/dev/null 2>&1; then
    PKG_MGR=dnf; PKG_UPDATE=""; PKG_INSTALL="dnf install -y -q"
  elif command -v yum >/dev/null 2>&1; then
    PKG_MGR=yum; PKG_UPDATE=""; PKG_INSTALL="yum install -y -q"
  elif command -v apk >/dev/null 2>&1; then
    PKG_MGR=apk; PKG_UPDATE="apk update -q"; PKG_INSTALL="apk add -q"
  elif command -v pacman >/dev/null 2>&1; then
    PKG_MGR=pacman; PKG_UPDATE="pacman -Sy --noconfirm -q"
    PKG_INSTALL="pacman -S --noconfirm --needed -q"
  elif command -v zypper >/dev/null 2>&1; then
    PKG_MGR=zypper; PKG_UPDATE=""; PKG_INSTALL="zypper -q in -y"
  else
    die "no supported package manager (apt/dnf/yum/apk/pacman/zypper)"
  fi
}

# pkg name differs per distro; try the list in order and stop at the first hit
pkg_for(){
  for n in "$@"; do
    case "$PKG_MGR" in
      apt|zypper) apt-cache show "$n" >/dev/null 2>&1 && { printf '%s' "$n"; return 0; } ;;
      *) rpm -q "$n" >/dev/null 2>&1 || true; printf '%s' "$n"; return 0 ;;
    esac
  done
  printf '%s' "$1"
}

MISSING=""
need(){ MISSING="${MISSING:+$MISSING }$1"; }

check_prereqs(){
  MISSING=""          # re-probe from scratch, so this is safe to call twice
  command -v python3 >/dev/null 2>&1 || need python3
  # ss -K lives in iproute2; probe -K support, not just the binary
  if ! command -v ss >/dev/null 2>&1 || ! ss -K -h >/dev/null 2>&1; then
    need iproute2
  fi
  command -v iptables >/dev/null 2>&1 || need iptables
}

# systemd is the unit runner. There is no non-systemd fallback, but it must be
# checked AFTER packages are installed so the message is accurate.
require_systemd(){
  command -v systemctl >/dev/null 2>&1 \
    || die "systemd not found; this tool needs a systemd host (no fallback)"
}

missing_list(){ echo "$MISSING" | xargs 2>/dev/null || echo "$MISSING"; }

install_prereqs(){
  check_prereqs
  [ -n "$(missing_list)" ] || return 0
  local wanted="$MISSING"
  say "prerequisites missing:$wanted"
  detect_pkg
  say "installing via $PKG_MGR ..."
  local names=""
  for p in $wanted; do
    case "$p" in
      iproute2) names="$names $(pkg_for iproute2 iproute)" ;;
      iptables) names="$names $(pkg_for iptables iptables-legacy)" ;;
      python3)  names="$names $(pkg_for python3 python python3-minimal)" ;;
      *)        names="$names $p" ;;
    esac
  done
  [ -n "$PKG_UPDATE" ] && { $PKG_UPDATE >/dev/null 2>&1 || true; }
  local rc=0
  $PKG_INSTALL $names >/dev/null 2>&1 || rc=$?
  # the install must have produced commands that actually work, not just exit 0
  check_prereqs
  if [ -n "$(missing_list)" ]; then
    die "could not install:$MISSING  (try manually: $PKG_INSTALL$names)"
  fi
  [ "$rc" = 0 ] || say "  (package manager returned $rc but every tool now works; continuing)"
  say "prerequisites satisfied:$wanted"
}

# ---------- payload ----------
b64d(){ base64 -d > "$1"; }
echo '@WATCHER@' | b64d "$TMP/watcher.py"
echo '@ACCLOG@'  | b64d "$TMP/set_accesslog.py"
echo '@LOGROT@'  | b64d "$TMP/logrotate"

# ---------- detect the install ----------
detect(){
  DB=""; CONFIG=""; XRAY=""; APIPORT=""
  for d in /etc/x-ui /usr/local/etc/x-ui; do
    [ -f "$d/x-ui.db" ] && DB="$d/x-ui.db" && break
  done
  for d in /usr/local/x-ui/bin /usr/local/bin /opt/x-ui/bin; do
    [ -f "$d/config.json" ] && [ -z "$CONFIG" ] && CONFIG="$d/config.json"
    for b in xray-linux-amd64 xray; do
      [ -x "$d/$b" ] && [ -z "$XRAY" ] && XRAY="$d/$b"
    done
  done
  [ -n "$DB" ]     || die "3x-ui DB not found (looked in /etc/x-ui, /usr/local/etc/x-ui)"
  [ -n "$CONFIG" ] || die "xray config.json not found (looked in /usr/local/x-ui/bin, ...)"
  [ -n "$XRAY" ]   || die "xray binary not found"
  command -v python3 >/dev/null || die "python3 missing"
  # the API inbound is a dokodemo-door usually named 'api'; take its port
  APIPORT=$(python3 - "$CONFIG" <<'PY'
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: print(""); raise SystemExit
for i in d.get("inbounds") or []:
    if i.get("protocol")=="dokodemo-door" and i.get("listen") in ("127.0.0.1","::1"):
        print(i.get("port")); break
else: print("")
PY
)
  [ -n "$APIPORT" ] || APIPORT=62789
}

# ---------- actions ----------
do_status(){
  # probe without dying: --status must work even on a host that is not set up
  MISSING=""
  command -v python3 >/dev/null 2>&1 || need python3
  command -v ss >/dev/null 2>&1 && ss -K -h >/dev/null 2>&1 || need iproute2
  command -v iptables >/dev/null 2>&1 || need iptables
  command -v systemctl >/dev/null 2>&1 || MISSING="${MISSING:+$MISSING }systemd"
  local pre
  pre=$(missing_list)
  # detect, but report an incomplete host instead of aborting -- status is a
  # diagnostic, so it must be able to describe a broken install too
  DB=""; CONFIG=""; XRAY=""; APIPORT=""
  for d in /etc/x-ui /usr/local/etc/x-ui; do
    [ -f "$d/x-ui.db" ] && DB="$d/x-ui.db" && break
  done
  for d in /usr/local/x-ui/bin /usr/local/bin /opt/x-ui/bin; do
    [ -f "$d/config.json" ] && [ -z "$CONFIG" ] && CONFIG="$d/config.json"
    for b in xray-linux-amd64 xray; do
      [ -x "$d/$b" ] && [ -z "$XRAY" ] && XRAY="$d/$b"
    done
  done
  say "3x-ui cutoff -- status"
  say "  prereqs     : ${pre:-all present}"
  say "  version     : $( [ -f "$PY" ] && python3 "$PY" --version 2>/dev/null || echo 'not installed')"
  say "  service     : $(systemctl is-active xui-user-cutoff 2>/dev/null || echo inactive) / $(systemctl is-enabled xui-user-cutoff 2>/dev/null || echo disabled)"
  say "  db          : ${DB:-NOT FOUND}"
  say "  config      : ${CONFIG:-NOT FOUND}"
  say "  xray        : ${XRAY:-NOT FOUND}"
  if [ -z "$DB" ] || [ -z "$CONFIG" ] || [ -z "$XRAY" ]; then
    say ""
    say "  3x-ui is not installed on this host."
    say "  Install the panel first, create one inbound, then re-run this installer."
    return 1
  fi
  detect
  say "  api         : 127.0.0.1:$APIPORT"
  local acc
  acc=$(python3 - "$CONFIG" <<'PY'
import json,sys
try: print((json.load(open(sys.argv[1])).get("log") or {}).get("access") or "none")
except Exception: print("?")
PY
)
  say "  access log  : $acc"
  say "  flow drops  : $(iptables -S INPUT 2>/dev/null | grep -c 'xui-cutoff-flow' || true)"
  say "  ip drops    : $(iptables -S INPUT 2>/dev/null | grep -c 'xui-cutoff' || true)"
  [ -f /var/log/xui-user-cutoff.log ] && { say "  recent:"; tail -3 /var/log/xui-user-cutoff.log | sed 's/^/    /'; }
}

do_uninstall(){
  say "removing service and rules"
  systemctl stop xui-user-cutoff 2>/dev/null || true
  systemctl disable xui-user-cutoff 2>/dev/null || true
  rm -f "$UNIT" "$PY" "$ACCLOG_TOOL" "$LOGROTATE"
  systemctl daemon-reload 2>/dev/null || true
  # remove ONLY the rules this tool owns, by its comment tag
  iptables -S INPUT 2>/dev/null | grep 'xui-cutoff' | grep -v '^-N' \
    | sed 's/^-A/iptables -D/' | sh 2>/dev/null || true
  say "done (access log setting left as-is; use the panel to disable it)"
}

do_accesslog(){
  local mode="$1"
  [ -f "$ACCLOG_TOOL" ] || install -m 0755 "$TMP/set_accesslog.py" "$ACCLOG_TOOL"
  python3 "$ACCLOG_TOOL" --$mode
  say ""
  say "A running xray only picks up a log-path change on (re)start."
  say "NOTE: restarting x-ui also restarts xray AND may drop your SSH session if"
  say "      this host runs a tunnel (wstunnel etc). Reconnect if it closes."
  systemctl restart x-ui
  sleep 5
  local p
  p=$(python3 - "$CONFIG" <<'PY'
import json,sys
try: print((json.load(open(sys.argv[1])).get("log") or {}).get("access") or "")
except Exception: print("")
PY
)
  if [ -n "$p" ] && [ "$mode" = on ]; then
    [ -f "$p" ] && say "access log live: $p ($(wc -l < "$p") lines)" \
                || warn "configured but no file at $p yet (it appears on the first connection)"
  fi
}

do_install(){
  install_prereqs
  require_systemd
  detect
  say "3x-ui cutoff installer v$VERSION"
  say "  db     : $DB"
  say "  config : $CONFIG"
  say "  xray   : $XRAY"
  say "  api    : 127.0.0.1:$APIPORT"

  install -m 0755 "$TMP/watcher.py"     "$PY"
  install -m 0755 "$TMP/set_accesslog.py" "$ACCLOG_TOOL"
  install -m 0644 "$TMP/logrotate"      "$LOGROTATE"

  cat > "$CONF" <<CONF
# generated by install-xui-cutoff.sh v$VERSION
DB=$DB
CONFIG=$CONFIG
XRAY_BIN=$XRAY
API_ADDR=127.0.0.1:$APIPORT
LOG=/var/log/xui-user-cutoff.log
POLL=2
SS=ss
AWG=awg
WG=wg
# 1 = block UDP flows with REJECT+ICMP (instant for TCP-like clients, still
# bounded by the QUIC idle timeout for hysteria because QUIC ignores ICMP)
UDP_REJECT=1
CONF

  if [ ! -f "$UNIT" ]; then
    cat > "$UNIT" <<'UNITEOF'
[Unit]
Description=Cut live Xray sessions of finished 3x-ui clients (quota/expiry/disable)
After=network.target x-ui.service
Wants=x-ui.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/xui_user_cutoff.py
Restart=always
RestartSec=5
Nice=10
IOSchedulingClass=idle
CPUQuota=25%
MemoryMax=96M
MemorySwapMax=0
OOMScoreAdjust=500
KillMode=control-group
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNITEOF
  fi

  say ""
  say "selftest:"
  python3 "$PY" --selftest || die "selftest FAILED -- nothing started"

  systemctl daemon-reload
  systemctl enable xui-user-cutoff >/dev/null 2>&1 || true
  systemctl restart xui-user-cutoff
  sleep 3
  systemctl is-active --quiet xui-user-cutoff || die "service did not start (journalctl -u xui-user-cutoff)"
  say ""
  say "installed and running"
  do_status
}

MODE=install
for a in "$@"; do
  case "$a" in
    --with-accesslog) MODE=install-with-log ;;
    --accesslog-only) MODE=log-only ;;
    --uninstall)      MODE=uninstall ;;
    --status)         MODE=status ;;
    -h|--help)        sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown option: $a" ;;
  esac
done

case "$MODE" in
  status)   do_status ;;
  uninstall) do_uninstall ;;
  log-only) do_accesslog on ;;
  install)  do_install ;;
  install-with-log) do_install; say ""; do_accesslog on ;;
esac
'''

out = (TEMPLATE
       .replace("@VERSION@", VERSION)
       .replace("@WATCHER@", WATCHER)
       .replace("@ACCLOG@", ACCLOG)
       .replace("@LOGROT@", LOGROT))

path = os.path.join(SRC, "install-xui-cutoff.sh")
with open(path, "w") as fh:
    fh.write(out)
os.chmod(path, 0o755)
print("wrote %s (%d bytes)" % (path, len(out)))
