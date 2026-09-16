#!/bin/sh

PATH="/opt/bin:/opt/sbin:/sbin:/usr/sbin:/bin:/usr/bin:$PATH"

ROUTING_PATH="/opt/etc/xray/configs/05_routing.json"
OUTBOUNDS_PATH="/opt/etc/xray/configs/04_outbounds.json"
SINGBOX_PATH="/opt/etc/sing-box/xkeen.json"
STATE_PATH="/opt/share/xkeen-manager/xkeen-ui-state.json"
AUTOSELECT_CATALOG_PATH="/opt/share/xkeen-manager/autoselect-catalog.json"
AUTOSELECT_STATUS_PATH="/tmp/antigoblin-autoselect-status.json"
AUTOSELECT_SCRIPT="/opt/share/xkeen-manager/api/xkeen-autoselect.sh"
# Per-PID scratch paths. Two concurrent CGI processes MUST NOT share
# these — read_body writes to TMP_BODY BEFORE acquire_apply_lock, so if
# both used a fixed path client B would overwrite client A's body, and
# A (after taking the lock) would then `cp $TMP_BODY $STATE_PATH` and
# persist B's data under A's response. Same class of race exists for
# TMP_NEW/TMP_STATE. The $$ suffix makes each process self-contained.
TMP_BODY="/tmp/xkeen-routing-body-$$.json"
TMP_NEW="/tmp/xkeen-routing-new-$$.json"
TMP_STATE="/tmp/xkeen-state-new-$$.json"
TMP_AUTH_HEADERS="/tmp/xkeen-auth-headers-$$.txt"
LOG_PATH="/opt/var/log/xray-manual.log"
XRAY_BIN="/opt/sbin/xray"
SELFHEAL_PATH="/opt/share/xkeen-manager/api/xkeen-selfheal.sh"
TMP_RESTART_SCRIPT="/tmp/xkeen-apply-restart.sh"
RUNTIME_DIR="/opt/share/xkeen-manager/runtime"
XKEEN_MARK=""

XKEEN_RUNTIME_LOG="$LOG_PATH"
if [ -f "/opt/share/xkeen-manager/api/xkeen-runtime.sh" ]; then
  . "/opt/share/xkeen-manager/api/xkeen-runtime.sh"
elif [ -f "$(dirname "$0")/xkeen-runtime.sh" ]; then
  . "$(dirname "$0")/xkeen-runtime.sh"
fi

json_ok() {
  printf 'Status: 200 OK\r\n'
  printf 'Content-Type: application/json; charset=utf-8\r\n'
  printf 'Cache-Control: no-store\r\n'
  printf '\r\n'
  printf '%s\n' "$1"
}

json_err() {
  printf 'Status: 500 Internal Server Error\r\n'
  printf 'Content-Type: application/json; charset=utf-8\r\n'
  printf 'Cache-Control: no-store\r\n'
  printf '\r\n'
  printf '{"ok":false,"error":"%s"}\n' "$1"
}

json_unauthorized() {
  printf 'Status: 401 Unauthorized\r\n'
  printf 'Content-Type: application/json; charset=utf-8\r\n'
  printf 'Cache-Control: no-store\r\n'
  printf '\r\n'
  printf '{"ok":false,"error":"router ui authorization required"}\n'
}

json_invalid_credentials() {
  printf 'Status: 401 Unauthorized\r\n'
  printf 'Content-Type: application/json; charset=utf-8\r\n'
  printf 'Cache-Control: no-store\r\n'
  printf '\r\n'
  printf '{"ok":false,"error":"invalid router credentials"}\n'
}

read_body() {
  # Cap request bodies. uhttpd forwards CONTENT_LENGTH bytes into stdin,
  # so an authenticated attacker sending CONTENT_LENGTH=500MB would fill
  # /tmp (tmpfs) and OOM the router. Reject early on the declared header,
  # and use `head -c` as a belt-and-suspenders limit if the header lied.
  MAX_BODY=3145728
  DECLARED="${CONTENT_LENGTH:-0}"
  case "$DECLARED" in ''|*[!0-9]*) DECLARED=0 ;; esac
  if [ "$DECLARED" -gt "$MAX_BODY" ]; then
    printf 'Status: 413 Payload Too Large\r\n'
    printf 'Content-Type: application/json; charset=utf-8\r\n'
    printf 'Cache-Control: no-store\r\n'
    printf '\r\n'
    printf '{"ok":false,"error":"body too large (%s > %s bytes)"}\n' "$DECLARED" "$MAX_BODY"
    exit 0
  fi
  head -c "$MAX_BODY" > "$TMP_BODY"
}

# Parse HTTP Host header, stripping the port. Handles both plain hosts
# (`192.168.1.1:8899`) and bracketed IPv6 literals (`[fdxx::1]:8899` →
# `[fdxx::1]`). Plain `sed 's/:.*$//'` on the IPv6 form would leave `[`.
strip_host_port() {
  case "$1" in
    '')           printf '%s' "192.168.1.1" ;;
    '['*']:'*)    printf '%s' "${1%%]:*}]" ;;
    '['*']')      printf '%s' "$1" ;;
    *:*)          printf '%s' "${1%:*}" ;;
    *)            printf '%s' "$1" ;;
  esac
}

# Router auth endpoint (host used for wget http://…/auth calls).
# NEVER derived from HTTP_HOST — the client controls that header, so an
# attacker on LAN could set `Host: attacker.tld` and steer the session
# check to a server they own that replies "HTTP/1.1 200 OK" to any
# request, bypassing router auth entirely. Same channel also carries the
# challenge/password-hash exchange during login → offline brute-force.
# Resolution order:
#   1. ROUTER_AUTH_HOST in /opt/etc/antigoblin.conf (operator override,
#      hostname or bracketed IPv6; awk-validated, no `source`).
#   2. xkeen_lan_ip — LAN-side address of this Keenetic (already excludes
#      the WAN interface in double-NAT setups).
#   3. Hard fallback 192.168.1.1 (Keenetic factory default).
router_auth_endpoint() {
  if [ -f /opt/etc/antigoblin.conf ]; then
    OVERRIDE="$(/opt/bin/awk -F= '
      $1 == "ROUTER_AUTH_HOST" && $2 ~ /^[A-Za-z0-9._:\[\]-]+$/ {
        print $2; exit
      }
    ' /opt/etc/antigoblin.conf 2>/dev/null)"
    if [ -n "$OVERRIDE" ]; then
      printf '%s' "$OVERRIDE"
      return 0
    fi
  fi
  LAN="$(xkeen_lan_ip 2>/dev/null)"
  if [ -n "$LAN" ]; then
    printf '%s' "$LAN"
    return 0
  fi
  printf '%s' "192.168.1.1"
}

# Cross-process apply lock shared with selfheal. Uses xkeen_lock_acquire
# from xkeen-runtime.sh (PID-recycle-safe + race-window-safe). Wall-clock
# capped at 60s — well below uhttpd's `-t 120` CGI timeout, so the client
# gets a clean 503 instead of a 502 Bad Gateway from uhttpd cutting the
# CGI process mid-flight.
acquire_apply_lock() {
  wait_sec="${1:-60}"
  case "$wait_sec" in ''|*[!0-9]*) wait_sec=60 ;; esac
  deadline=$(( $(date +%s) + wait_sec ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if xkeen_lock_acquire; then
      trap 'xkeen_lock_release' EXIT INT TERM
      return 0
    fi
    sleep 1
  done
  return 1
}

require_apply_lock() {
  wait_sec="${1:-60}"
  if ! acquire_apply_lock "$wait_sec"; then
    printf 'Status: 503 Service Unavailable\r\n'
    printf 'Content-Type: application/json; charset=utf-8\r\n'
    printf 'Cache-Control: no-store\r\n'
    printf '\r\n'
    printf '{"ok":false,"error":"apply lock busy; another apply or selfheal cycle in progress"}\n'
    rm -f "$TMP_BODY" 2>/dev/null || true
    exit 0
  fi
}

release_apply_lock_now() {
  if type xkeen_lock_release >/dev/null 2>&1; then
    xkeen_lock_release 2>/dev/null || true
  fi
  trap - EXIT INT TERM
}

restart_xray() {
  type xkeen_ensure_socks_inbound_ip >/dev/null 2>&1 && xkeen_ensure_socks_inbound_ip
  # Graceful stop first: wait for the old xray to actually exit before
  # starting a new one. Old code did `killall xray; sleep 2` which can
  # leave the previous process still holding :61219 when a new one tries
  # to bind, especially at high fd count (the very scenario UI-restart is
  # meant to recover from).
  OLD_XRAY_PID="$(get_xray_pid 2>/dev/null)"
  killall xray 2>/dev/null || true
  if [ -n "$OLD_XRAY_PID" ]; then
    j=0
    while [ $j -lt 8 ] && kill -0 "$OLD_XRAY_PID" 2>/dev/null; do
      sleep 1
      j=$((j + 1))
    done
    # PID may have been recycled during the poll window (BusyBox has a
    # small PID space and short-lived sh scripts churn PIDs fast). Only
    # SIGKILL if the process still identifies as xray.
    if kill -0 "$OLD_XRAY_PID" 2>/dev/null; then
      CMDLINE="$(tr '\0' ' ' < "/proc/$OLD_XRAY_PID/cmdline" 2>/dev/null || true)"
      case "$CMDLINE" in
        *xray*) kill -9 "$OLD_XRAY_PID" 2>/dev/null || true ;;
      esac
    fi
  else
    sleep 2
  fi
  rm -f /opt/var/run/xray-ui.pid /opt/var/run/xray.pid 2>/dev/null || true
  XRAY_LOCATION_ASSET=/opt/etc/xray/dat XRAY_LOCATION_CONFDIR=/opt/etc/xray/configs \
    /opt/sbin/start-stop-daemon -S -b -m -p /opt/var/run/xray-ui.pid -x "$XRAY_BIN" -- run >>"$LOG_PATH" 2>&1
  # Poll for :61219 instead of a fixed sleep. Capped at ~12s for slow flash.
  i=0
  while [ $i -lt 12 ]; do
    if netstat -lnpt 2>/dev/null | grep -q ':61219 '; then
      # A UI-driven restart also counts as a real restart from selfheal's
      # perspective: publish the stamp AND clear any streak/backoff state
      # that selfheal was tracking for auto-restarts. Also clear the
      # "please restart xray" sentinel so selfheal doesn't do a redundant
      # second restart on its next tick.
      date +%s > /tmp/xkeen-xray-restart-last.ts 2>/dev/null || true
      rm -f /tmp/xkeen-xray-start-fail-streak /tmp/xkeen-xray-start-backoff.ts /tmp/xkeen-needs-xray-restart 2>/dev/null || true
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

restart_singbox() {
  # Do not call rc.func `restart` synchronously from CGI. On some builds it
  # can wait indefinitely for an old QUIC session/process, leaving the apply
  # lock held even after the browser aborts. Bound stop/start ourselves.
  OLD_SB_PID="$(pidof sing-box 2>/dev/null | /opt/bin/awk '{ print $1 }')"
  killall sing-box 2>/dev/null || true
  i=0
  while [ $i -lt 6 ] && [ -n "$OLD_SB_PID" ] && kill -0 "$OLD_SB_PID" 2>/dev/null; do
    sleep 1
    i=$((i + 1))
  done
  if [ -n "$OLD_SB_PID" ] && kill -0 "$OLD_SB_PID" 2>/dev/null; then
    CMDLINE="$(tr '\0' ' ' < "/proc/$OLD_SB_PID/cmdline" 2>/dev/null || true)"
    case "$CMDLINE" in *sing-box*) kill -9 "$OLD_SB_PID" 2>/dev/null || true ;; esac
  fi
  rm -f /opt/var/run/sing-box.pid 2>/dev/null || true
  [ -x /opt/sbin/sing-box ] || return 1
  [ -f /opt/etc/sing-box/xkeen.json ] || return 1
  /opt/sbin/start-stop-daemon -S -b -m -p /opt/var/run/sing-box.pid -x /opt/sbin/sing-box -- run -c /opt/etc/sing-box/xkeen.json >>/opt/var/log/sing-box-xkeen.log 2>&1 || return 1

  i=0
  while [ $i -lt 10 ]; do
    if pidof sing-box >/dev/null 2>&1 && netstat -lnpu 2>/dev/null | grep -q ':61221 '; then
      # sing-box-backed TCP protocols additionally require the xray relay.
      if /opt/bin/jq -e '.inbounds[]? | select(.listen_port == 61225)' /opt/etc/sing-box/xkeen.json >/dev/null 2>&1; then
        netstat -lnpt 2>/dev/null | grep -q ':61225 ' || { sleep 1; i=$((i + 1)); continue; }
      fi
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

validate_singbox_file() {
  SB_FILE="$1"
  SB_LOG="${2:-/tmp/xkeen-singbox-check-$$.log}"
  /opt/sbin/sing-box check -c "$SB_FILE" >"$SB_LOG" 2>&1 &
  SB_CHECK_PID=$!
  i=0
  while [ $i -lt 12 ] && kill -0 "$SB_CHECK_PID" 2>/dev/null; do
    sleep 1
    i=$((i + 1))
  done
  if kill -0 "$SB_CHECK_PID" 2>/dev/null; then
    kill "$SB_CHECK_PID" 2>/dev/null || true
    sleep 1
    kill -9 "$SB_CHECK_PID" 2>/dev/null || true
    wait "$SB_CHECK_PID" 2>/dev/null || true
    printf '%s\n' 'sing-box check timed out after 12s' >>"$SB_LOG"
    return 124
  fi
  wait "$SB_CHECK_PID"
}

validate_confdir() {
  # xray -test should normally finish in <1s, but a damaged filesystem or
  # plugin/asset lookup must not hold the CGI/apply lock forever.
  CHECK_LOG="/tmp/xkeen-xray-check-$$.log"
  /opt/sbin/xray run -test -confdir /opt/etc/xray/configs >"$CHECK_LOG" 2>&1 &
  CHECK_PID=$!
  i=0
  while [ $i -lt 12 ] && kill -0 "$CHECK_PID" 2>/dev/null; do
    sleep 1
    i=$((i + 1))
  done
  if kill -0 "$CHECK_PID" 2>/dev/null; then
    kill "$CHECK_PID" 2>/dev/null || true
    sleep 1
    kill -9 "$CHECK_PID" 2>/dev/null || true
    wait "$CHECK_PID" 2>/dev/null || true
    rm -f "$CHECK_LOG"
    return 124
  fi
  wait "$CHECK_PID"
  RC=$?
  rm -f "$CHECK_LOG"
  return "$RC"
}

validate_xray_candidate() {
  OUT_FILE="$1"
  ROUTE_FILE="$2"
  CAND_DIR="/tmp/xkeen-xray-candidate-$$"
  CHECK_LOG="/tmp/xkeen-xray-candidate-check-$$.log"
  rm -rf "$CAND_DIR" 2>/dev/null || true
  mkdir -p "$CAND_DIR" || return 1

  for CFG in /opt/etc/xray/configs/*.json; do
    [ -f "$CFG" ] || continue
    BASE="${CFG##*/}"
    case "$BASE" in
      04_outbounds.json|05_routing.json) continue ;;
    esac
    cp "$CFG" "$CAND_DIR/$BASE" || { rm -rf "$CAND_DIR"; return 1; }
  done
  cp "$OUT_FILE" "$CAND_DIR/04_outbounds.json" || { rm -rf "$CAND_DIR"; return 1; }
  cp "$ROUTE_FILE" "$CAND_DIR/05_routing.json" || { rm -rf "$CAND_DIR"; return 1; }

  /opt/sbin/xray run -test -confdir "$CAND_DIR" >"$CHECK_LOG" 2>&1 &
  CHECK_PID=$!
  i=0
  while [ $i -lt 12 ] && kill -0 "$CHECK_PID" 2>/dev/null; do
    sleep 1
    i=$((i + 1))
  done
  if kill -0 "$CHECK_PID" 2>/dev/null; then
    kill "$CHECK_PID" 2>/dev/null || true
    sleep 1
    kill -9 "$CHECK_PID" 2>/dev/null || true
    wait "$CHECK_PID" 2>/dev/null || true
    printf '%s\n' 'xray candidate check timed out after 12s' >>"$CHECK_LOG"
    rm -rf "$CAND_DIR"
    return 124
  fi
  wait "$CHECK_PID"
  RC=$?
  rm -rf "$CAND_DIR"
  return "$RC"
}

get_xray_pid() {
  PID="$(netstat -lnpt 2>/dev/null | /opt/bin/awk '/:61219 / && /\/xray/ { split($NF, p, "/"); print p[1]; exit }')"
  [ -n "$PID" ] && { printf '%s\n' "$PID"; return 0; }

  for PID in $(pidof xray 2>/dev/null); do
    CMDLINE="$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null || true)"
    case "$CMDLINE" in
      *" -test "*) continue ;;
    esac
    printf '%s\n' "$PID"
    return 0
  done
}

repair_runtime() {
  # We are called with acquire_apply_lock already held (POST branch).
  # DO NOT fork selfheal --force here — it will try to grab the same
  # shared lock, see our PID owning it (comm=`sh`), consider us alive,
  # and quietly exit 0 without doing any repair. The UI would then get
  # {"ok":true} while the runtime is untouched. Run the repair inline
  # under our own lock instead.
  if type xkeen_repair_hooks >/dev/null 2>&1; then
    xkeen_repair_hooks || return 1
    restart_xray || return 1
    return 0
  fi

  # Fallback if xkeen-runtime.sh could not be sourced. This path is only
  # reachable when the CGI itself isn't holding a lock (i.e. never today),
  # so it's safe to fork the selfheal here.
  if [ -x "$SELFHEAL_PATH" ]; then
    "$SELFHEAL_PATH" --force >/dev/null 2>&1
    return $?
  fi

  return 1
}

get_kind() {
  case "$QUERY_STRING" in
    kind=state|*'&kind=state'|kind=state'&'*)
      printf 'state'
      ;;
    kind=repair-runtime|*'&kind=repair-runtime'|kind=repair-runtime'&'*)
      printf 'repair-runtime'
      ;;
    kind=login|*'&kind=login'|kind=login'&'*)
      printf 'login'
      ;;
    kind=logout|*'&kind=logout'|kind=logout'&'*)
      printf 'logout'
      ;;
    kind=outbounds|*'&kind=outbounds'|kind=outbounds'&'*)
      printf 'outbounds'
      ;;
    kind=probe|*'&kind=probe'|kind=probe'&'*)
      printf 'probe'
      ;;
    kind=health|*'&kind=health'|kind=health'&'*)
      printf 'health'
      ;;
    kind=logs|*'&kind=logs'|kind=logs'&'*)
      printf 'logs'
      ;;
    kind=restart-svc|*'&kind=restart-svc'|kind=restart-svc'&'*)
      printf 'restart-svc'
      ;;
    kind=stack-info|*'&kind=stack-info'|kind=stack-info'&'*)
      printf 'stack-info'
      ;;
    kind=subscription-fetch|*'&kind=subscription-fetch'|kind=subscription-fetch'&'*)
      printf 'subscription-fetch'
      ;;
    kind=singbox|*'&kind=singbox'|kind=singbox'&'*)
      printf 'singbox'
      ;;
    kind=autoselect-catalog|*'&kind=autoselect-catalog'|kind=autoselect-catalog'&'*)
      printf 'autoselect-catalog'
      ;;
    kind=autoselect-status|*'&kind=autoselect-status'|kind=autoselect-status'&'*)
      printf 'autoselect-status'
      ;;
    kind=autoselect-run|*'&kind=autoselect-run'|kind=autoselect-run'&'*)
      printf 'autoselect-run'
      ;;
    kind=apply-runtime|*'&kind=apply-runtime'|kind=apply-runtime'&'*)
      printf 'apply-runtime'
      ;;
    *)
      printf 'routing'
      ;;
  esac
}

parse_qs_param() {
  PARAM_NAME="$1"
  printf '%s' "${QUERY_STRING:-}" | /opt/bin/awk -v want="$PARAM_NAME" '
    {
      count=split($0, parts, "&")
      for (i=1; i<=count; i++) {
        eqpos=index(parts[i], "=")
        if (eqpos == 0) continue
        key=substr(parts[i], 1, eqpos-1)
        val=substr(parts[i], eqpos+1)
        if (key == want) { print val; exit }
      }
    }
  '
}

load_vpn_endpoint() {
  VPN_HOST=""
  VPN_PORT=0
  VPN_SNI=""
  VPN_PROC="xray"
  VPN_PID="${XRAY_PID:-}"

  if [ -f "$OUTBOUNDS_PATH" ] && command -v /opt/bin/jq >/dev/null 2>&1; then
    VPN_HOST="$(/opt/bin/jq -r '.outbounds[]?|select(.tag=="vless-reality")|.settings.vnext[0].address // ""' "$OUTBOUNDS_PATH" 2>/dev/null | head -1)"
    VPN_PORT="$(/opt/bin/jq -r '.outbounds[]?|select(.tag=="vless-reality")|.settings.vnext[0].port // 0' "$OUTBOUNDS_PATH" 2>/dev/null | head -1)"
    VPN_SNI="$(/opt/bin/jq -r '.outbounds[]?|select(.tag=="vless-reality")|(.streamSettings.realitySettings.serverName // .streamSettings.tlsSettings.serverName // "")' "$OUTBOUNDS_PATH" 2>/dev/null | head -1)"
  fi

  # For sing-box-backed protocols Xray's stable "vless-reality" tag is a
  # loopback SOCKS bridge and therefore has no vnext endpoint. Read the real
  # remote from sing-box instead so health/diagnostics don't report 0:0.
  if [ -z "$VPN_HOST" ] && [ -f /opt/etc/sing-box/xkeen.json ] && command -v /opt/bin/jq >/dev/null 2>&1; then
    VPN_HOST="$(/opt/bin/jq -r '.outbounds[]?|select(.tag=="proxy")|.server // ""' /opt/etc/sing-box/xkeen.json 2>/dev/null | head -1)"
    VPN_PORT="$(/opt/bin/jq -r '.outbounds[]?|select(.tag=="proxy")|.server_port // 0' /opt/etc/sing-box/xkeen.json 2>/dev/null | head -1)"
    VPN_SNI="$(/opt/bin/jq -r '.outbounds[]?|select(.tag=="proxy")|.tls.server_name // ""' /opt/etc/sing-box/xkeen.json 2>/dev/null | head -1)"
    # sing-box >= 1.13 represents WireGuard as an endpoint, not an outbound.
    # Endpoint tags are valid routing targets, so read the first peer for
    # health metrics when the active proxy is endpoint-backed.
    if [ -z "$VPN_HOST" ]; then
      VPN_HOST="$(/opt/bin/jq -r '.endpoints[]?|select(.tag=="proxy")|.peers[0].address // ""' /opt/etc/sing-box/xkeen.json 2>/dev/null | head -1)"
      VPN_PORT="$(/opt/bin/jq -r '.endpoints[]?|select(.tag=="proxy")|.peers[0].port // 0' /opt/etc/sing-box/xkeen.json 2>/dev/null | head -1)"
    fi
    if [ -n "$VPN_HOST" ]; then
      VPN_PROC="sing-box"
      VPN_PID="${SB_PID:-}"
    fi
  fi

  case "$VPN_PORT" in ''|*[!0-9]*) VPN_PORT=0 ;; esac
}

emit_health() {
  XRAY_PID="$(get_xray_pid)"
  SB_PID="$(pidof sing-box 2>/dev/null | /opt/bin/awk '{ print $1 }')"
  SELFHEAL_PID="$(cat /opt/var/run/antigoblin-selfheal-loop.pid 2>/dev/null | /opt/bin/awk 'NR==1 && $0 ~ /^[0-9]+$/ { print }')"
  AUTOSELECT_PID="$(cat /opt/var/run/antigoblin-autoselect.pid 2>/dev/null | /opt/bin/awk 'NR==1 && $0 ~ /^[0-9]+$/ { print }')"
  if [ -n "$SELFHEAL_PID" ] && ! kill -0 "$SELFHEAL_PID" 2>/dev/null; then
    SELFHEAL_PID=""
  fi
  if [ -n "$AUTOSELECT_PID" ] && ! kill -0 "$AUTOSELECT_PID" 2>/dev/null; then
    AUTOSELECT_PID=""
  fi

  XRAY_TCP_OK=0
  netstat -lnpt 2>/dev/null | grep -q ':61219 ' && XRAY_TCP_OK=1
  XRAY_RELAY_OK=0
  netstat -lnpu 2>/dev/null | grep -q '127.0.0.1:62640 ' && XRAY_RELAY_OK=1
  SB_LISTEN_OK=0
  netstat -lnpu 2>/dev/null | grep -q ':61221 ' && SB_LISTEN_OK=1

  # UDP route is only required when the active profile has at least one
  # non-bypass/non-direct group enabled. If not, the ipset, the mangle
  # jump and the ip rule are DELIBERATELY absent — reporting "fail" for
  # those in that case is a false positive. Return "na" so the UI can
  # show a neutral "not needed" badge instead of red.
  UDP_ROUTE_NEEDED=0
  if type xkeen_udp_config_enabled >/dev/null 2>&1 && xkeen_udp_config_enabled 2>/dev/null; then
    UDP_ROUTE_NEEDED=1
  fi

  UDP_MARK_HEX="${XKEEN_UDP_MARK:-0x111}"
  UDP_TABLE="${XKEEN_UDP_TABLE:-111}"

  # State strings: "ok" | "fail" | "na". Consumed by the frontend as
  # tri-state so it can paint "na" grey ("not required") instead of red.
  if [ "$UDP_ROUTE_NEEDED" = "1" ]; then
    TPROXY_AT_END="fail"
    iptables -t mangle -S PREROUTING 2>/dev/null | tail -1 | grep -q 'xkeen_udp_route' && TPROXY_AT_END="ok"
    IP_RULE_MASKED="fail"
    ip rule show 2>/dev/null | grep -qE "fwmark ${UDP_MARK_HEX}(/${UDP_MARK_HEX})? (lookup|table) ${UDP_TABLE}" && IP_RULE_MASKED="ok"
    UDP_IPSET_OK="fail"
    ipset list xkeen_udp_route -terse >/dev/null 2>&1 && UDP_IPSET_OK="ok"
  else
    TPROXY_AT_END="na"
    IP_RULE_MASKED="na"
    UDP_IPSET_OK="na"
  fi
  BYPASS_IPSET_OK="fail"
  ipset list xkeen_bypass -terse >/dev/null 2>&1 && BYPASS_IPSET_OK="ok"

  UDP_IPSET_SIZE=0
  if [ "$UDP_IPSET_OK" = "ok" ]; then
    UDP_IPSET_SIZE="$(ipset list xkeen_udp_route 2>/dev/null | /opt/bin/awk '/^Members:/ { m=1; next } m && NF { c++ } END { print c+0 }')"
  fi
  BYPASS_IPSET_SIZE=0
  if [ "$BYPASS_IPSET_OK" = "ok" ]; then
    BYPASS_IPSET_SIZE="$(ipset list xkeen_bypass 2>/dev/null | /opt/bin/awk '/^Members:/ { m=1; next } m && NF { c++ } END { print c+0 }')"
  fi

  # TCP interception is the prerequisite for routing.json to matter. A valid
  # Xray config with a missing PREROUTING hook proxies exactly zero LAN TCP.
  TCP_CAPTURE_OK="fail"
  TCP_CAPTURE_PACKETS=0
  if iptables -t nat -S PREROUTING 2>/dev/null | grep -q -- '-j xkeen'; then
    TCP_CAPTURE_OK="ok"
    TCP_CAPTURE_PACKETS="$(iptables -t nat -L PREROUTING -v -n -x 2>/dev/null | /opt/bin/awk '$0 ~ /xkeen/ {sum += $1} END {print sum+0}')"
  fi
  case "$TCP_CAPTURE_PACKETS" in ''|*[!0-9]*) TCP_CAPTURE_PACKETS=0 ;; esac

  # FD count
  XRAY_FD=0
  XRAY_FD_LIMIT=0
  if [ -n "$XRAY_PID" ] && [ -d "/proc/$XRAY_PID/fd" ]; then
    XRAY_FD="$(ls "/proc/$XRAY_PID/fd" 2>/dev/null | wc -l | tr -d ' ')"
    XRAY_FD_LIMIT="$(grep 'Max open files' "/proc/$XRAY_PID/limits" 2>/dev/null | /opt/bin/awk '{ print $4; exit }')"
    case "$XRAY_FD_LIMIT" in ''|unlimited) XRAY_FD_LIMIT=0 ;; esac
  fi
  case "$XRAY_FD"       in ''|*[!0-9]*) XRAY_FD=0 ;; esac
  case "$XRAY_FD_LIMIT" in ''|*[!0-9]*) XRAY_FD_LIMIT=0 ;; esac

  # Conntrack
  CT_COUNT="$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || echo 0)"
  CT_MAX="$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || echo 0)"
  case "$CT_COUNT" in ''|*[!0-9]*) CT_COUNT=0 ;; esac
  case "$CT_MAX"   in ''|*[!0-9]*) CT_MAX=0 ;; esac

  # VPN socket metrics
  load_vpn_endpoint
  VPN_IP=""
  VPN_ESTABLISHED=0
  VPN_FIN_WAIT=0
  VPN_ORPHAN_FIN=0
  VPN_TOTAL=0
  if [ -n "$VPN_HOST" ] && [ "$VPN_PORT" -gt 0 ]; then
    if type xkeen_resolve_ipv4 >/dev/null 2>&1; then
      VPN_IP="$(xkeen_resolve_ipv4 "$VPN_HOST" | head -1)"
    else
      VPN_IP="$(nslookup "$VPN_HOST" 2>/dev/null | /opt/bin/awk '/^Name:/{seen=1;next} seen&&/^Address [0-9]+:/{print $3;exit} seen&&/^Address:/{print $2;exit}' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -1)"
    fi
  fi
  if [ -n "$VPN_PID" ] && [ -n "$VPN_IP" ] && [ "$VPN_PORT" -gt 0 ]; then
    # Match PID/process exactly on the last netstat field. The real remote
    # process is Xray for VLESS/VMess and sing-box for bridged protocols.
    SOCK_LINES="$(netstat -anp 2>/dev/null | /opt/bin/awk -v pid="$VPN_PID" -v proc="$VPN_PROC" -v ep="${VPN_IP}:${VPN_PORT}" '
      $NF == pid "/" proc && ($4 == ep || $5 == ep) { print }
    ')"
    VPN_TOTAL="$(printf '%s\n' "$SOCK_LINES" | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')"
    VPN_ESTABLISHED="$(printf '%s\n' "$SOCK_LINES" | grep -c 'ESTABLISHED' || true)"
    VPN_FIN_WAIT="$(printf '%s\n' "$SOCK_LINES" | grep -cE 'FIN_WAIT1|FIN_WAIT2' || true)"
    VPN_ORPHAN_FIN="$(netstat -anp 2>/dev/null | /opt/bin/awk -v ep="${VPN_IP}:${VPN_PORT}" '
      ($4 == ep || $5 == ep) && ($6 == "FIN_WAIT1" || $6 == "FIN_WAIT2") && $NF == "-" { c++ }
      END { print c+0 }
    ')"
  fi
  case "$VPN_ESTABLISHED" in ''|*[!0-9]*) VPN_ESTABLISHED=0 ;; esac
  case "$VPN_FIN_WAIT"    in ''|*[!0-9]*) VPN_FIN_WAIT=0 ;; esac
  case "$VPN_ORPHAN_FIN"  in ''|*[!0-9]*) VPN_ORPHAN_FIN=0 ;; esac
  case "$VPN_TOTAL"       in ''|*[!0-9]*) VPN_TOTAL=0 ;; esac

  # Compute health status (mirrors selfheal thresholds)
  HEALTH_STATUS="ok"
  if [ -z "$XRAY_PID" ]; then
    HEALTH_STATUS="xray_down"
  elif [ "$VPN_PROC" = "sing-box" ] && [ -z "$SB_PID" ]; then
    HEALTH_STATUS="singbox_down"
  elif [ "$XRAY_FD" -ge 600 ]; then
    HEALTH_STATUS="fd_critical"
  elif [ "$VPN_ORPHAN_FIN" -ge 30 ]; then
    HEALTH_STATUS="vpn_orphan_fin_critical"
  elif [ "$VPN_FIN_WAIT" -ge 50 ]; then
    HEALTH_STATUS="vpn_fin_critical"
  elif [ "$XRAY_FD" -ge 400 ]; then
    HEALTH_STATUS="fd_warn"
  elif [ "$VPN_ORPHAN_FIN" -ge 20 ]; then
    HEALTH_STATUS="vpn_orphan_fin_warn"
  elif [ "$VPN_FIN_WAIT" -ge 20 ]; then
    HEALTH_STATUS="vpn_fin_warn"
  fi
  if [ "$CT_MAX" -gt 0 ]; then
    CT_PCT=$(( CT_COUNT * 100 / CT_MAX ))
    if [ "$CT_PCT" -ge 95 ] && [ "$HEALTH_STATUS" = "ok" ]; then HEALTH_STATUS="conntrack_critical"; fi
    if [ "$CT_PCT" -ge 85 ] && [ "$HEALTH_STATUS" = "ok" ]; then HEALTH_STATUS="conntrack_warn"; fi
  fi

  XRAY_RUN=$([ -n "$XRAY_PID" ] && printf 'true' || printf 'false')
  SB_RUN=$([ -n "$SB_PID" ] && printf 'true' || printf 'false')
  SH_RUN=$([ -n "$SELFHEAL_PID" ] && printf 'true' || printf 'false')
  AS_RUN=$([ -n "$AUTOSELECT_PID" ] && printf 'true' || printf 'false')

  PAYLOAD="$(/opt/bin/jq -n \
    --argjson xray_run "$XRAY_RUN" \
    --arg xray_pid "${XRAY_PID:-}" \
    --argjson xray_tcp "$XRAY_TCP_OK" \
    --argjson xray_relay "$XRAY_RELAY_OK" \
    --argjson sb_run "$SB_RUN" \
    --arg sb_pid "${SB_PID:-}" \
    --argjson sb_listen "$SB_LISTEN_OK" \
    --argjson sh_run "$SH_RUN" \
    --arg sh_pid "${SELFHEAL_PID:-}" \
    --argjson as_run "$AS_RUN" \
    --arg as_pid "${AUTOSELECT_PID:-}" \
    --arg tproxy_end "$TPROXY_AT_END" \
    --arg ip_rule_masked "$IP_RULE_MASKED" \
    --arg udp_ipset_ok "$UDP_IPSET_OK" \
    --arg bypass_ipset_ok "$BYPASS_IPSET_OK" \
    --arg tcp_capture_ok "$TCP_CAPTURE_OK" \
    --argjson tcp_capture_packets "$TCP_CAPTURE_PACKETS" \
    --argjson udp_ipset_size "$UDP_IPSET_SIZE" \
    --argjson bypass_ipset_size "$BYPASS_IPSET_SIZE" \
    --argjson xray_fd "$XRAY_FD" \
    --argjson xray_fd_limit "$XRAY_FD_LIMIT" \
    --argjson ct_count "$CT_COUNT" \
    --argjson ct_max "$CT_MAX" \
    --argjson vpn_established "$VPN_ESTABLISHED" \
    --argjson vpn_fin_wait "$VPN_FIN_WAIT" \
    --argjson vpn_orphan_fin "$VPN_ORPHAN_FIN" \
    --argjson vpn_total "$VPN_TOTAL" \
    --arg vpn_host "${VPN_HOST:-}" \
    --argjson vpn_port "${VPN_PORT:-0}" \
    --arg vpn_process "${VPN_PROC:-xray}" \
    --arg health_status "$HEALTH_STATUS" \
    '{
      ok: true,
      healthStatus: $health_status,
      services: {
        xray:    { running: $xray_run, pid: $xray_pid, listenTcp: ($xray_tcp == 1), listenRelayUdp: ($xray_relay == 1) },
        singbox: { running: $sb_run, pid: $sb_pid, listenUdp: ($sb_listen == 1) },
        selfheal:{ running: $sh_run, pid: $sh_pid },
        autoselect:{ running: $as_run, pid: $as_pid }
      },
      checks: {
        tproxyRuleAtEnd:   $tproxy_end,
        ipRuleMasked:      $ip_rule_masked,
        udpIpsetExists:    $udp_ipset_ok,
        bypassIpsetExists: $bypass_ipset_ok,
        tcpCaptureHook: $tcp_capture_ok,
        tcpCapturePackets: $tcp_capture_packets
      },
      ipsetSize: { udpRoute: $udp_ipset_size, bypass: $bypass_ipset_size },
      xrayFd: { count: $xray_fd, limit: $xray_fd_limit },
      conntrack: { count: $ct_count, max: $ct_max },
      vpnTunnel: {
        host: $vpn_host,
        port: $vpn_port,
        process: $vpn_process,
        established: $vpn_established,
        finWait: $vpn_fin_wait,
        orphanFin: $vpn_orphan_fin,
        total: $vpn_total
      }
    }')"

  printf 'Status: 200 OK\r\n'
  printf 'Content-Type: application/json; charset=utf-8\r\n'
  printf 'Cache-Control: no-store\r\n'
  printf '\r\n'
  printf '%s\n' "$PAYLOAD"
  exit 0
}

emit_logs() {
  SVC="$(parse_qs_param svc)"
  N="$(parse_qs_param n)"
  case "$N" in
    ''|*[!0-9]*) N=100 ;;
  esac
  if [ "$N" -gt 1000 ]; then N=1000; fi

  case "$SVC" in
    xray)     LOG_FILE="$LOG_PATH" ;;
    singbox)  LOG_FILE="/opt/var/log/sing-box-xkeen.log" ;;
    selfheal) LOG_FILE="/opt/var/log/xkeen-selfheal.log" ;;
    autoselect) LOG_FILE="/opt/var/log/antigoblin-autoselect.log" ;;
    health)   LOG_FILE="/opt/var/log/xkeen-health.log" ;;
    sysctl)   LOG_FILE="/opt/var/log/xkeen-sysctl.log" ;;
    fd-dump)
      LOG_FILE="$(ls -t /opt/var/log/xray-fd-dump-*.txt 2>/dev/null | head -n 1)"
      ;;
    *)
      json_err "unknown svc"
      exit 0
      ;;
  esac

  printf 'Status: 200 OK\r\n'
  printf 'Content-Type: text/plain; charset=utf-8\r\n'
  printf 'Cache-Control: no-store\r\n'
  printf '\r\n'
  if [ -z "$LOG_FILE" ]; then
    printf '(no fd-dump file present yet — none has been triggered since boot)\n'
  elif [ -f "$LOG_FILE" ]; then
    if [ "$SVC" = "fd-dump" ]; then
      printf '# %s\n\n' "$LOG_FILE"
      cat "$LOG_FILE" 2>/dev/null
    else
      tail -n "$N" "$LOG_FILE" 2>/dev/null
    fi
  else
    printf '(log file %s does not exist)\n' "$LOG_FILE"
  fi
  exit 0
}

probe_proxy_exit_ip() {
  # Test the actual active outbound, not the VPN server endpoint. The SOCKS
  # inbound is explicitly routed to tag vless-reality, so this works for
  # both native Xray VLESS/VMess and sing-box-backed protocols.
  [ -x /opt/bin/curl ] || { printf ''; return 0; }
  EXIT_IP="$('/opt/bin/curl' -fsS --socks5-hostname 127.0.0.1:61080 --connect-timeout 3 --max-time 6 https://api.ipify.org 2>/dev/null | tr -d '\r\n ' | head -c 80)"
  case "$EXIT_IP" in
    ''|*[!0-9a-fA-F:.]*) printf '' ;;
    *) printf '%s' "$EXIT_IP" ;;
  esac
}

emit_stack_info() {
  XRAY_PID="$(get_xray_pid)"
  SB_PID="$(pidof sing-box 2>/dev/null | /opt/bin/awk '{ print $1 }')"
  AG_VER="$(head -n 1 /opt/share/xkeen-manager/VERSION 2>/dev/null | tr -d '\r\n')"
  XRAY_VER="$(/opt/sbin/xray version 2>/dev/null | head -n 1 | /opt/bin/awk '{print $2}')"
  SB_VER="$(/opt/sbin/sing-box version 2>/dev/null | head -n 1 | /opt/bin/awk '{print $3}')"
  KERNEL="$(uname -r 2>/dev/null)"
  HOSTNAME_S="$(uname -n 2>/dev/null)"
  UPTIME_SEC="$(/opt/bin/awk '{ printf "%d", int($1) }' /proc/uptime 2>/dev/null)"
  case "$UPTIME_SEC" in ''|*[!0-9]*) UPTIME_SEC=0 ;; esac

  load_vpn_endpoint
  VPN_IP=""
  if [ -n "$VPN_HOST" ]; then
    if type xkeen_resolve_ipv4 >/dev/null 2>&1; then
      VPN_IP="$(xkeen_resolve_ipv4 "$VPN_HOST" | head -1)"
    else
      VPN_IP="$(nslookup "$VPN_HOST" 2>/dev/null | /opt/bin/awk '
        /^Name:/ { seen=1; next }
        seen && /^Address [0-9]+: / { print $3; exit }
        seen && /^Address: / { print $2; exit }
      ' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -1)"
    fi
  fi

  VPN_EXIT_IP="$(probe_proxy_exit_ip)"

  WAN_IFACE="$(ip route show default 2>/dev/null | /opt/bin/awk '/^default/{print $5; exit}')"
  WAN_IP=""
  if [ -n "$WAN_IFACE" ]; then
    WAN_IP="$(ip addr show "$WAN_IFACE" 2>/dev/null | /opt/bin/awk '/inet /{print $2; exit}' | cut -d/ -f1)"
  fi
  GW="$(ip route show default 2>/dev/null | /opt/bin/awk '/^default/{print $3; exit}')"
  LAN_NET="$(ip route show 2>/dev/null | /opt/bin/awk '/scope link/ && /^(192\.168\.|10\.|172\.)/ {print $1; exit}')"

  POLICY_BLOCK="$(ndmc -c 'show ip policy' 2>/dev/null)"
  # Format A (newer): one-line "policy, name = Policy42, description = xkeen:Home"
  # Format B (older): multi-line with separate "name: Policy42" / "description: xkeen:Home"
  # Use the single source of truth from xkeen-runtime.sh to guarantee that
  # CGI (this) and runtime (xkeen_get_mark/xkeen_ensure_policy) agree on
  # which policy the health UI shows vs which one selfheal actually manages.
  DESC_MATCH="${XKEEN_POLICY_DESC_RE:-description[[:space:]]*[=:][[:space:]]*\"?xkeen(\$|[\":,[:space:]])}"
  POLICY_LINE="$(printf '%s\n' "$POLICY_BLOCK" | grep -E "$DESC_MATCH" | head -n 1)"
  POLICY_NAME="$(printf '%s' "$POLICY_LINE" | sed -n 's/.*name *= *\([^,]*\).*/\1/p' | sed 's/[[:space:]]*$//')"
  POLICY_DESC="$(printf '%s' "$POLICY_LINE" | sed -n 's/.*description *= *\([^,]*\).*/\1/p' | sed 's/[[:space:]]*$//' | sed 's/:[[:space:]]*$//' | sed 's/^xkeen:[[:space:]]*//' | sed 's/^xkeen$//')"
  if [ -z "$POLICY_NAME" ]; then
    POLICY_NAME="$(printf '%s\n' "$POLICY_BLOCK" | /opt/bin/awk -v pat="$DESC_MATCH" '/^[[:space:]]*name:/{n=$2} $0 ~ pat {print n; exit}')"
  fi
  if [ -z "$POLICY_DESC" ]; then
    POLICY_DESC="$(printf '%s\n' "$POLICY_BLOCK" | /opt/bin/awk -F ': ' -v pat="$DESC_MATCH" '$0 ~ pat {gsub(/^[[:space:]]+/,"",$2); print $2; exit}')"
  fi
  # Use the runtime helper — it already handles Format A (mark on the same
  # line as description) and Format B (mark on a later line), and reset on
  # new `policy,`. Local awk here had the same `next` bug that iter 2
  # fixed in xkeen_get_mark, so keeping a private copy re-introduced the
  # UI-vs-runtime split-brain.
  if type xkeen_get_mark >/dev/null 2>&1; then
    XKEEN_MARK_VAL="$(xkeen_get_mark 2>/dev/null)"
  else
    XKEEN_MARK_VAL=""
  fi

  MEM_AVAIL_KB="$(grep '^MemAvailable:' /proc/meminfo 2>/dev/null | /opt/bin/awk '{print $2}')"
  MEM_TOTAL_KB="$(grep '^MemTotal:' /proc/meminfo 2>/dev/null | /opt/bin/awk '{print $2}')"
  DISK_LINE="$(df -k /opt 2>/dev/null | tail -n 1)"
  DISK_TOTAL_KB="$(printf '%s' "$DISK_LINE" | /opt/bin/awk '{print $2}')"
  DISK_USED_KB="$(printf '%s' "$DISK_LINE" | /opt/bin/awk '{print $3}')"
  DISK_AVAIL_KB="$(printf '%s' "$DISK_LINE" | /opt/bin/awk '{print $4}')"
  DISK_MOUNT="$(printf '%s' "$DISK_LINE" | /opt/bin/awk '{print $NF}')"
  CT_COUNT="$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || echo 0)"
  CT_MAX="$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || echo 0)"
  XRAY_PID_S="$(get_xray_pid)"
  XRAY_FD_COUNT_S=0
  XRAY_FD_LIMIT_S=0
  if [ -n "$XRAY_PID_S" ] && [ -d "/proc/$XRAY_PID_S/fd" ]; then
    XRAY_FD_COUNT_S="$(ls "/proc/$XRAY_PID_S/fd" 2>/dev/null | wc -l | tr -d ' ')"
    XRAY_FD_LIMIT_S="$(grep 'Max open files' "/proc/$XRAY_PID_S/limits" 2>/dev/null | /opt/bin/awk '{print $4}')"
  fi
  case "$MEM_AVAIL_KB"  in ''|*[!0-9]*) MEM_AVAIL_KB=0 ;; esac
  case "$MEM_TOTAL_KB"  in ''|*[!0-9]*) MEM_TOTAL_KB=0 ;; esac
  case "$CT_COUNT"      in ''|*[!0-9]*) CT_COUNT=0 ;; esac
  case "$CT_MAX"        in ''|*[!0-9]*) CT_MAX=0 ;; esac
  case "$XRAY_FD_COUNT_S" in ''|*[!0-9]*) XRAY_FD_COUNT_S=0 ;; esac
  case "$XRAY_FD_LIMIT_S" in ''|*[!0-9]*) XRAY_FD_LIMIT_S=0 ;; esac
  case "$DISK_TOTAL_KB" in ''|*[!0-9]*) DISK_TOTAL_KB=0 ;; esac
  case "$DISK_USED_KB"  in ''|*[!0-9]*) DISK_USED_KB=0 ;; esac
  case "$DISK_AVAIL_KB" in ''|*[!0-9]*) DISK_AVAIL_KB=0 ;; esac

  PAYLOAD="$(/opt/bin/jq -n \
    --arg ag_ver "$AG_VER" \
    --arg xray_ver "$XRAY_VER" \
    --arg sb_ver "$SB_VER" \
    --arg kernel "$KERNEL" \
    --arg hostname "$HOSTNAME_S" \
    --argjson uptime_sec "$UPTIME_SEC" \
    --arg vpn_host "$VPN_HOST" \
    --argjson vpn_port "$VPN_PORT" \
    --arg vpn_sni "$VPN_SNI" \
    --arg vpn_endpoint_ip "$VPN_IP" \
    --arg vpn_exit_ip "$VPN_EXIT_IP" \
    --arg wan_iface "$WAN_IFACE" \
    --arg wan_ip "$WAN_IP" \
    --arg lan_net "$LAN_NET" \
    --arg gw "$GW" \
    --arg policy_name "$POLICY_NAME" \
    --arg policy_desc "$POLICY_DESC" \
    --arg xkeen_mark "$XKEEN_MARK_VAL" \
    --argjson mem_avail_kb "$MEM_AVAIL_KB" \
    --argjson mem_total_kb "$MEM_TOTAL_KB" \
    --argjson ct_count "$CT_COUNT" \
    --argjson ct_max "$CT_MAX" \
    --argjson xray_fd "$XRAY_FD_COUNT_S" \
    --argjson xray_fd_limit "$XRAY_FD_LIMIT_S" \
    --argjson disk_total_kb "$DISK_TOTAL_KB" \
    --argjson disk_used_kb "$DISK_USED_KB" \
    --argjson disk_avail_kb "$DISK_AVAIL_KB" \
    --arg disk_mount "$DISK_MOUNT" \
    '{
      ok: true,
      versions: { antigoblin: $ag_ver, xray: $xray_ver, singbox: $sb_ver, kernel: $kernel, hostname: $hostname, uptimeSec: $uptime_sec },
      vpn:      { host: $vpn_host, port: $vpn_port, sni: $vpn_sni, endpointIp: $vpn_endpoint_ip, exitIp: $vpn_exit_ip },
      network:  { wanIface: $wan_iface, wanIp: $wan_ip, gateway: $gw, lanNet: $lan_net },
      xkeen:    { policyName: $policy_name, policyDescription: $policy_desc, mark: $xkeen_mark, tproxyUdp: 61221, redirectTcp: 61219, ssRelay: "127.0.0.1:62640" },
      runtime:  { selfhealIntervalSec: 15, logRotateInterval: "daily", backupRetention: 5, fdWarn: 400, fdCritical: 600 },
      resources:{ memAvailKb: $mem_avail_kb, memTotalKb: $mem_total_kb, conntrackCount: $ct_count, conntrackMax: $ct_max, xrayFd: $xray_fd, xrayFdLimit: $xray_fd_limit, diskTotalKb: $disk_total_kb, diskUsedKb: $disk_used_kb, diskAvailKb: $disk_avail_kb, diskMount: $disk_mount }
    }')"

  printf 'Status: 200 OK\r\n'
  printf 'Content-Type: application/json; charset=utf-8\r\n'
  printf 'Cache-Control: no-store\r\n'
  printf '\r\n'
  printf '%s\n' "$PAYLOAD"
  exit 0
}

restart_service() {
  SVC="$(json_field 'svc')"
  case "$SVC" in
    xray)
      if restart_xray; then
        json_ok "{\"ok\":true,\"service\":\"xray\"}"
      else
        json_err "xray restart failed"
      fi
      ;;
    singbox)
      if restart_singbox; then
        json_ok "{\"ok\":true,\"service\":\"singbox\"}"
      else
        json_err "singbox restart timed out or required listener did not bind"
      fi
      ;;
    selfheal)
      if [ -x /opt/etc/init.d/S25antigoblin-selfheal ]; then
        /opt/etc/init.d/S25antigoblin-selfheal restart >/dev/null 2>&1
        sleep 1
        json_ok "{\"ok\":true,\"service\":\"selfheal\"}"
      else
        json_err "selfheal init script missing"
      fi
      ;;
    *)
      json_err "unknown service"
      ;;
  esac
  rm -f "$TMP_BODY"
  exit 0
}

emit_file() {
  FILE_PATH="$1"
  if [ ! -f "$FILE_PATH" ]; then
    printf 'Status: 404 Not Found\r\n'
    printf 'Content-Type: application/json; charset=utf-8\r\n'
    printf 'Cache-Control: no-store\r\n'
    printf '\r\n'
    printf '{"ok":false,"error":"file not found"}\n'
    exit 0
  fi
  printf 'Status: 200 OK\r\n'
  printf 'Content-Type: application/json; charset=utf-8\r\n'
  printf 'Cache-Control: no-store\r\n'
  printf '\r\n'
  cat "$FILE_PATH"
  exit 0
}

json_field() {
  FIELD_NAME="$1"
  sed -n "s/.*\"${FIELD_NAME}\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" "$TMP_BODY" | head -n 1
}

valid_probe_address() {
  printf '%s' "$1" | grep -Eq '^[A-Za-z0-9.-]+$'
}

valid_probe_port() {
  printf '%s' "$1" | grep -Eq '^[0-9]+$' || return 1
  [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

# Fetch a subscription URL and return its body base64-encoded for JSON-safe
# transport. The endpoint is intentionally dumb: it does NOT decode or parse
# the subscription content; the client decodes base64 and parses URIs.
# This keeps the server simple and avoids assumptions about format.
#
# Constraints:
#   - URL must use https:// (no plain http to prevent token leaks)
#   - URL length capped to avoid pathological inputs
#   - Response capped at 256KB and 10s wall time (DoS mitigation)
#   - Prefers curl; falls back to wget only if it has HTTPS support
fetch_subscription() {
  URL="$(json_field url)"

  if [ -z "$URL" ]; then
    json_err "missing url field"
    rm -f "$TMP_BODY"
    exit 0
  fi

  case "$URL" in
    https://*) ;;
    *)
      json_err "url must use https://"
      rm -f "$TMP_BODY"
      exit 0
      ;;
  esac

  URL_LEN="${#URL}"
  if [ "$URL_LEN" -gt 2048 ]; then
    json_err "url too long ($URL_LEN > 2048)"
    rm -f "$TMP_BODY"
    exit 0
  fi

  # SSRF guard: refuse hosts on private / loopback / link-local space.
  # An authenticated UI operator otherwise gets a fetch-through-router
  # primitive to probe internal services (`https://192.168.1.1/rci/…`,
  # `https://127.0.0.1/…`). Uses a substring match on the URL host part —
  # skips resolve-and-recheck (an extra DNS round-trip we'd have to
  # timeout-cap for a marginal gain against active DNS-rebinding attacks
  # which are already limited to a single 10s fetch here).
  HOST_PART="${URL#https://}"
  HOST_PART="${HOST_PART%%/*}"
  HOST_PART="${HOST_PART%%\?*}"
  HOST_PART="${HOST_PART%%\#*}"
  HOST_PART="${HOST_PART##*@}"
  case "$HOST_PART" in
    '['*']:'*) HOST_ONLY="${HOST_PART%%]:*}]" ;;
    '['*']')   HOST_ONLY="$HOST_PART" ;;
    *:*)       HOST_ONLY="${HOST_PART%:*}" ;;
    *)         HOST_ONLY="$HOST_PART" ;;
  esac
  case "$HOST_ONLY" in
    127.*|10.*|192.168.*|169.254.*|0.0.0.0|::1|'[::1]'|localhost|localhost.*|*.localhost|'[fc'*|'[fd'*|'[fe8'*|'[fe9'*|'[fea'*|'[feb'*)
      json_err "subscription url points to a private / loopback address"
      rm -f "$TMP_BODY"
      exit 0
      ;;
    172.*)
      SECOND="${HOST_ONLY#172.}"
      SECOND="${SECOND%%.*}"
      case "$SECOND" in
        16|17|18|19|20|21|22|23|24|25|26|27|28|29|30|31)
          json_err "subscription url points to a private address"
          rm -f "$TMP_BODY"
          exit 0
          ;;
      esac
      ;;
  esac

  FETCHER=""
  if [ -x /opt/bin/curl ]; then
    FETCHER="curl"
  elif [ -x /opt/bin/wget ] && /opt/bin/wget --version 2>&1 | grep -qiE '\+https|gnutls|openssl|ssl/tls'; then
    FETCHER="wget"
  else
    json_err "no https-capable fetcher (install curl: opkg install curl)"
    rm -f "$TMP_BODY"
    exit 0
  fi

  # Per-PID scratch — subscription-fetch runs WITHOUT the apply lock, so
  # two concurrent refreshes on a fixed path would swap their bodies:
  # process A's `base64 -w 0 < TMP_FETCH` could encode B's contents and
  # return VLESS-UUIDs / vmess-passwords from subscription B in the
  # response to A.
  TMP_FETCH="/tmp/xkeen-sub-fetch-$$.raw"
  TMP_FETCH_ERR="/tmp/xkeen-sub-fetch-$$.err"
  rm -f "$TMP_FETCH" "$TMP_FETCH_ERR"

  if [ "$FETCHER" = "curl" ]; then
    /opt/bin/curl -fsSL \
      --max-time 10 \
      --max-filesize 262144 \
      -A 'AntiGoblin/1.0' \
      -o "$TMP_FETCH" \
      "$URL" 2>"$TMP_FETCH_ERR"
    RC=$?
  else
    /opt/bin/wget -q \
      --timeout=10 \
      --tries=1 \
      -O "$TMP_FETCH" \
      "$URL" 2>"$TMP_FETCH_ERR"
    RC=$?
  fi

  if [ "$RC" -ne 0 ] || [ ! -s "$TMP_FETCH" ]; then
    ERR="$(tr -d '\r' < "$TMP_FETCH_ERR" 2>/dev/null | tr '\n' ' ' | sed 's/"/\\"/g' | cut -c1-200)"
    json_err "fetch failed (rc=$RC, fetcher=$FETCHER): $ERR"
    rm -f "$TMP_BODY" "$TMP_FETCH" "$TMP_FETCH_ERR"
    exit 0
  fi

  SIZE="$(wc -c < "$TMP_FETCH" 2>/dev/null || echo 0)"

  # wget has no --max-filesize equivalent; enforce the cap after the fact
  # so a hostile / compromised subscription endpoint can't stream tens of
  # MB into /tmp within the 10s timeout window and OOM tmpfs. curl was
  # already capped via --max-filesize 262144.
  if [ "$FETCHER" = "wget" ] && [ "$SIZE" -gt 262144 ]; then
    json_err "subscription response too large ($SIZE > 262144 bytes)"
    rm -f "$TMP_BODY" "$TMP_FETCH" "$TMP_FETCH_ERR"
    exit 0
  fi

  ENCODED="$(/opt/bin/base64 -w 0 < "$TMP_FETCH" 2>/dev/null || /opt/bin/base64 < "$TMP_FETCH" | tr -d '\n\r ')"

  if [ -z "$ENCODED" ]; then
    json_err "base64 encoding produced empty output (size=$SIZE)"
    rm -f "$TMP_BODY" "$TMP_FETCH" "$TMP_FETCH_ERR"
    exit 0
  fi

  json_ok "{\"ok\":true,\"size\":${SIZE},\"fetcher\":\"${FETCHER}\",\"raw\":\"${ENCODED}\"}"
  rm -f "$TMP_BODY" "$TMP_FETCH" "$TMP_FETCH_ERR"
  exit 0
}

router_auth_login() {
  REQUEST_HOST="$(router_auth_endpoint)"
  REQUEST_UA="${HTTP_USER_AGENT:-xkeen-manager}"

  LOGIN_B64="$(json_field 'loginB64')"
  PASSWORD_B64="$(json_field 'passwordB64')"

  if [ -z "$LOGIN_B64" ] || [ -z "$PASSWORD_B64" ]; then
    json_err "invalid login payload"
    rm -f "$TMP_BODY" "$TMP_AUTH_HEADERS"
    exit 0
  fi

  LOGIN="$(printf '%s' "$LOGIN_B64" | /opt/bin/base64 -d 2>/dev/null)"
  PASSWORD="$(printf '%s' "$PASSWORD_B64" | /opt/bin/base64 -d 2>/dev/null)"

  if [ -z "$LOGIN" ] || [ -z "$PASSWORD" ]; then
    json_err "failed to decode credentials"
    rm -f "$TMP_BODY" "$TMP_AUTH_HEADERS"
    exit 0
  fi

  AUTH_GET_HEADERS="$(wget -S -O - --timeout=5 --tries=1 \
    --header="Host: $REQUEST_HOST" \
    --header="User-Agent: $REQUEST_UA" \
    "http://$REQUEST_HOST/auth" 2>&1 | head -c 8192)"

  REALM="$(printf '%s' "$AUTH_GET_HEADERS" | sed -n 's/.*realm="\([^"]*\)".*/\1/p' | head -n 1)"
  CHALLENGE="$(printf '%s' "$AUTH_GET_HEADERS" | sed -n 's/.*challenge="\([^"]*\)".*/\1/p' | head -n 1)"
  SESSION_ID="$(printf '%s' "$AUTH_GET_HEADERS" | sed -n 's/.*session_id="\([^"]*\)".*/\1/p' | head -n 1)"
  SESSION_COOKIE="$(printf '%s' "$AUTH_GET_HEADERS" | sed -n 's/.*session_cookie="\([^"]*\)".*/\1/p' | head -n 1)"

  if [ -z "$REALM" ] || [ -z "$CHALLENGE" ] || [ -z "$SESSION_ID" ] || [ -z "$SESSION_COOKIE" ]; then
    json_err "failed to read router auth challenge"
    rm -f "$TMP_BODY" "$TMP_AUTH_HEADERS"
    exit 0
  fi

  LOGIN_MD5="$(printf '%s' "${LOGIN}:${REALM}:${PASSWORD}" | /opt/bin/md5sum | /opt/bin/awk '{print $1}')"
  LOGIN_SHA256="$(printf '%s' "${CHALLENGE}${LOGIN_MD5}" | /opt/bin/sha256sum | /opt/bin/awk '{print $1}')"
  AUTH_PAYLOAD="$(/opt/bin/jq -cn --arg login "$LOGIN" --arg password "$LOGIN_SHA256" '{login:$login, password:$password}')"

  AUTH_POST_HEADERS="$(wget -S -O - --timeout=5 --tries=1 \
    --header="Host: $REQUEST_HOST" \
    --header="Cookie: ${SESSION_COOKIE}=${SESSION_ID}" \
    --header="User-Agent: $REQUEST_UA" \
    --header="Content-Type: application/json; charset=utf-8" \
    --post-data="$AUTH_PAYLOAD" \
    "http://$REQUEST_HOST/auth" 2>&1 | head -c 8192)"

  printf '%s' "$AUTH_POST_HEADERS" | grep -q 'HTTP/1\.[01] 200' || {
    json_invalid_credentials
    rm -f "$TMP_BODY" "$TMP_AUTH_HEADERS"
    exit 0
  }

  printf 'Status: 200 OK\r\n'
  printf 'Content-Type: application/json; charset=utf-8\r\n'
  printf 'Cache-Control: no-store\r\n'
  printf 'Set-Cookie: %s=%s; Path=/; SameSite=Strict; Max-Age=300\r\n' "$SESSION_COOKIE" "$SESSION_ID"
  printf '\r\n'
  /opt/bin/jq -cn --arg login "$LOGIN" '{ok:true, login:$login}'
  rm -f "$TMP_BODY" "$TMP_AUTH_HEADERS"
  exit 0
}

router_auth_logout() {
  REQUEST_HOST="$(router_auth_endpoint)"
  REQUEST_COOKIE="${HTTP_COOKIE:-}"
  SESSION_COOKIE_NAME="$(printf '%s' "$REQUEST_COOKIE" | sed -n 's/^\([^=;[:space:]]*\)=.*/\1/p' | head -n 1)"

  printf 'Status: 200 OK\r\n'
  printf 'Content-Type: application/json; charset=utf-8\r\n'
  printf 'Cache-Control: no-store\r\n'
  if [ -n "$SESSION_COOKIE_NAME" ]; then
    printf 'Set-Cookie: %s=; Path=/; SameSite=Strict; Max-Age=0\r\n' "$SESSION_COOKIE_NAME"
  fi
  printf '\r\n'
  /opt/bin/jq -cn --arg host "$REQUEST_HOST" '{ok:true, host:$host}'
  rm -f "$TMP_BODY" "$TMP_AUTH_HEADERS"
  exit 0
}

require_router_session() {
  REQUEST_HOST="$(router_auth_endpoint)"
  REQUEST_COOKIE="${HTTP_COOKIE:-}"
  REQUEST_UA="${HTTP_USER_AGENT:-xkeen-manager}"

  if [ -z "$REQUEST_COOKIE" ]; then
    json_unauthorized
    exit 0
  fi

  AUTH_RESPONSE="$(wget -S -O - --timeout=5 --tries=1 \
    --header="Host: $REQUEST_HOST" \
    --header="Cookie: $REQUEST_COOKIE" \
    --header="User-Agent: $REQUEST_UA" \
    "http://$REQUEST_HOST/auth" 2>&1 | head -c 8192)"

  printf '%s' "$AUTH_RESPONSE" | grep -q 'HTTP/1\.[01] 200' || {
    json_unauthorized
    exit 0
  }
}

case "$REQUEST_METHOD" in
  GET)
    require_router_session
    KIND="$(get_kind)"
    if [ "$KIND" = "state" ]; then
      emit_file "$STATE_PATH"
    fi
    if [ "$KIND" = "outbounds" ]; then
      emit_file "$OUTBOUNDS_PATH"
    fi
    if [ "$KIND" = "autoselect-status" ]; then
      if [ -f "$AUTOSELECT_STATUS_PATH" ]; then
        emit_file "$AUTOSELECT_STATUS_PATH"
      fi
      json_ok '{"ok":true,"phase":"idle","message":"no latency run yet","switched":false,"bestId":"","bestLatencyMs":null,"currentId":"","currentLatencyMs":null,"updatedAt":0,"results":[]}'
      exit 0
    fi
    if [ "$KIND" = "health" ]; then
      emit_health
    fi
    if [ "$KIND" = "logs" ]; then
      emit_logs
    fi
    if [ "$KIND" = "stack-info" ]; then
      emit_stack_info
    fi
    emit_file "$ROUTING_PATH"
    ;;
  POST)
    KIND="$(get_kind)"
    # Auth before body for anything that isn't login/logout — otherwise an
    # unauthenticated client can waste CGI processes uploading a multi-megabyte body
    # only to fail the session check afterward. Login/logout themselves
    # legitimately need the body before their own auth logic.
    case "$KIND" in
      login|logout)
        read_body
        ;;
      *)
        require_router_session
        read_body
        ;;
    esac
    BODY_SIZE="$(wc -c < "$TMP_BODY" 2>/dev/null)"

    if [ "$KIND" = "login" ]; then
      router_auth_login
    fi

    if [ "$KIND" = "logout" ]; then
      router_auth_logout
    fi

    case "$KIND" in
      probe|subscription-fetch|autoselect-run|autoselect-catalog)
        ;;
      state|apply-runtime)
        # Interactive state/apply calls must fail fast if self-heal currently
        # owns the lock. Waiting a full minute looked exactly like a frozen UI.
        # State still uses the shared lock so it cannot race an auto-selector
        # switch that updates activeProxyId.
        require_apply_lock 8
        ;;
      *)
        require_apply_lock
        ;;
    esac

    if [ "$KIND" = "apply-runtime" ]; then
      AP_STATE="/tmp/xkeen-apply-state-$$.json"
      AP_OUT="/tmp/xkeen-apply-outbounds-$$.json"
      AP_SB="/tmp/xkeen-apply-singbox-$$.json"
      AP_ROUTE="/tmp/xkeen-apply-routing-$$.json"
      AP_SB_LOG="/tmp/xkeen-apply-singbox-check-$$.log"
      AP_XRAY_LOG="/tmp/xkeen-xray-candidate-check-$$.log"

      cleanup_apply_tmp() {
        rm -f "$AP_STATE" "$AP_OUT" "$AP_SB" "$AP_ROUTE" "$AP_SB_LOG" "$AP_XRAY_LOG" 2>/dev/null || true
      }

      if ! /opt/bin/jq -c '.state' "$TMP_BODY" > "$AP_STATE" 2>/dev/null          || ! /opt/bin/jq -c '.outbounds' "$TMP_BODY" > "$AP_OUT" 2>/dev/null          || ! /opt/bin/jq -c '.singbox' "$TMP_BODY" > "$AP_SB" 2>/dev/null          || ! /opt/bin/jq -c '.routing' "$TMP_BODY" > "$AP_ROUTE" 2>/dev/null; then
        cleanup_apply_tmp
        json_err "invalid apply-runtime payload"
        rm -f "$TMP_BODY"
        exit 0
      fi

      if ! /opt/bin/jq -e 'type == "object" and (.profiles | type == "array")' "$AP_STATE" >/dev/null 2>&1; then
        cleanup_apply_tmp; json_err "invalid state in apply-runtime"; rm -f "$TMP_BODY"; exit 0
      fi
      if ! /opt/bin/jq -e '.outbounds | type == "array" and (map(.tag) | index("vless-reality") != null)' "$AP_OUT" >/dev/null 2>&1; then
        cleanup_apply_tmp; json_err "invalid xray outbounds in apply-runtime"; rm -f "$TMP_BODY"; exit 0
      fi
      if ! /opt/bin/jq -e '(.outbounds | type == "array") and (.inbounds | type == "array")' "$AP_SB" >/dev/null 2>&1; then
        cleanup_apply_tmp; json_err "invalid sing-box config in apply-runtime"; rm -f "$TMP_BODY"; exit 0
      fi
      if ! /opt/bin/jq -e '.routing.rules | type == "array"' "$AP_ROUTE" >/dev/null 2>&1; then
        cleanup_apply_tmp; json_err "invalid routing config in apply-runtime"; rm -f "$TMP_BODY"; exit 0
      fi

      if [ -x /opt/sbin/sing-box ] && ! validate_singbox_file "$AP_SB" "$AP_SB_LOG"; then
        AP_ERR="$(tail -n 4 "$AP_SB_LOG" 2>/dev/null | tr '\n' ' ' | tr '"\\' "'/" | cut -c1-360)"
        cleanup_apply_tmp
        json_err "sing-box candidate check failed: $AP_ERR"
        rm -f "$TMP_BODY"
        exit 0
      fi

      if ! validate_xray_candidate "$AP_OUT" "$AP_ROUTE"; then
        AP_ERR="$(tail -n 5 "$AP_XRAY_LOG" 2>/dev/null | tr '\n' ' ' | tr '"\\' "'/" | cut -c1-360)"
        cleanup_apply_tmp
        json_err "xray candidate check failed: $AP_ERR"
        rm -f "$TMP_BODY"
        exit 0
      fi

      TS="$(date +%Y%m%d-%H%M%S)"
      STATE_BAK="${STATE_PATH}.bak-ui-${TS}"
      OUT_BAK="${OUTBOUNDS_PATH}.bak-ui-${TS}"
      SB_BAK="${SINGBOX_PATH}.bak-ui-${TS}"
      ROUTE_BAK="${ROUTING_PATH}.bak-ui-${TS}"
      cp "$STATE_PATH" "$STATE_BAK" 2>/dev/null || true
      cp "$OUTBOUNDS_PATH" "$OUT_BAK" 2>/dev/null || true
      cp "$SINGBOX_PATH" "$SB_BAK" 2>/dev/null || true
      cp "$ROUTING_PATH" "$ROUTE_BAK" 2>/dev/null || true

      rollback_apply_runtime() {
        [ -f "$STATE_BAK" ] && cp "$STATE_BAK" "$STATE_PATH" 2>/dev/null || true
        [ -f "$OUT_BAK" ] && cp "$OUT_BAK" "$OUTBOUNDS_PATH" 2>/dev/null || true
        [ -f "$SB_BAK" ] && cp "$SB_BAK" "$SINGBOX_PATH" 2>/dev/null || true
        [ -f "$ROUTE_BAK" ] && cp "$ROUTE_BAK" "$ROUTING_PATH" 2>/dev/null || true
      }

      STATE_STAGE="${STATE_PATH}.new-apply-$$"
      OUT_STAGE="${OUTBOUNDS_PATH}.new-apply-$$"
      SB_STAGE="${SINGBOX_PATH}.new-apply-$$"
      ROUTE_STAGE="${ROUTING_PATH}.new-apply-$$"
      if ! cp "$AP_STATE" "$STATE_STAGE"          || ! cp "$AP_OUT" "$OUT_STAGE"          || ! cp "$AP_SB" "$SB_STAGE"          || ! cp "$AP_ROUTE" "$ROUTE_STAGE"; then
        rm -f "$STATE_STAGE" "$OUT_STAGE" "$SB_STAGE" "$ROUTE_STAGE"
        cleanup_apply_tmp
        json_err "failed to stage apply-runtime files"
        rm -f "$TMP_BODY"
        exit 0
      fi

      COMMIT_OK=1
      mv "$OUT_STAGE" "$OUTBOUNDS_PATH" || COMMIT_OK=0
      [ "$COMMIT_OK" = "1" ] && mv "$SB_STAGE" "$SINGBOX_PATH" || COMMIT_OK=0
      [ "$COMMIT_OK" = "1" ] && mv "$ROUTE_STAGE" "$ROUTING_PATH" || COMMIT_OK=0
      [ "$COMMIT_OK" = "1" ] && mv "$STATE_STAGE" "$STATE_PATH" || COMMIT_OK=0
      rm -f "$STATE_STAGE" "$OUT_STAGE" "$SB_STAGE" "$ROUTE_STAGE" 2>/dev/null || true
      if [ "$COMMIT_OK" != "1" ]; then
        rollback_apply_runtime
        cleanup_apply_tmp
        json_err "failed to commit apply-runtime files; previous configs restored"
        rm -f "$TMP_BODY"
        exit 0
      fi
      chmod 600 "$STATE_PATH" 2>/dev/null || true

      if ! restart_singbox; then
        rollback_apply_runtime
        restart_singbox >/dev/null 2>&1 || true
        restart_xray >/dev/null 2>&1 || true
        cleanup_apply_tmp
        json_err "sing-box restart failed; previous configs restored"
        rm -f "$TMP_BODY"
        exit 0
      fi
      if ! restart_xray; then
        rollback_apply_runtime
        restart_singbox >/dev/null 2>&1 || true
        restart_xray >/dev/null 2>&1 || true
        cleanup_apply_tmp
        json_err "xray restart failed; previous configs restored"
        rm -f "$TMP_BODY"
        exit 0
      fi

      CAPTURE_READY=false
      UDP_CAPTURE_READY=true
      UDP_CAPTURE_REQUIRED=false
      if type xkeen_ensure_tcp_capture_hook >/dev/null 2>&1 && xkeen_ensure_tcp_capture_hook; then
        CAPTURE_READY=true
      fi

      # Catch-all VPN profiles (fallbackOutbound=vless-reality) must capture
      # UDP immediately too. Otherwise browsers can keep QUIC/HTTP3 on the WAN
      # while TCP is already going through VPN. This path is intentionally
      # synchronous and cheap: xkeen_build_udp_route_ipset uses 0.0.0.0/0 split
      # into two ranges and skips per-domain DNS resolution for catch-all.
      if /opt/bin/jq -e '
        (.activeProfileId // "") as $id
        | .profiles[]? | select(.id == $id)
        | .fallbackOutbound == "vless-reality"
      ' "$STATE_PATH" >/dev/null 2>&1; then
        UDP_CAPTURE_REQUIRED=true
        if ! type xkeen_apply_udp_route >/dev/null 2>&1 || ! xkeen_apply_udp_route; then
          UDP_CAPTURE_READY=false
        fi
      fi

      # Keep self-heal alive after an apply. Auto-select is restarted only
      # after the browser writes the fresh autoselect catalog (see that branch
      # below), otherwise a just-started loop can race on stale server configs.
      [ -x /opt/etc/init.d/S25antigoblin-selfheal ] && /opt/etc/init.d/S25antigoblin-selfheal start >/dev/null 2>&1 || true

      cleanup_apply_tmp
      rm -f "$TMP_BODY"

      # Release before forcing the heavy DNS/ipset repair. The force-run takes
      # the same lock itself; doing this while our CGI still owns it would make
      # it immediately no-op. The browser gets its success response without
      # waiting for DNS resolution, while the TCP capture hook is already live.
      release_apply_lock_now
      if [ -x "$SELFHEAL_PATH" ]; then
        ( exec >/dev/null 2>&1 </dev/null; sleep 1; "$SELFHEAL_PATH" --force || true ) &
      fi

      if [ "$CAPTURE_READY" != "true" ]; then
        json_err "VPN configs are valid and services restarted, but the xkeen TCP capture hook could not be installed; check Keenetic xkeen policy/iptables"
      elif [ "$UDP_CAPTURE_REQUIRED" = "true" ] && [ "$UDP_CAPTURE_READY" != "true" ]; then
        json_err "TCP capture is ready, but catch-all VPN requires UDP/QUIC TPROXY and it could not be installed; check xt_TPROXY/ip rule support"
      else
        json_ok "{\"ok\":true,\"restarted\":true,\"tcpCaptureReady\":true,\"udpCaptureRequired\":$UDP_CAPTURE_REQUIRED,\"udpCaptureReady\":$UDP_CAPTURE_READY,\"backgroundRepair\":true}"
      fi
      exit 0
    fi

    if [ "$KIND" = "state" ]; then
      if ! /opt/bin/jq -e 'type == "object" and has("profiles")' "$TMP_BODY" >/dev/null 2>&1; then
        cp "$TMP_BODY" /tmp/xkeen-routing-invalid.json 2>/dev/null || true
        json_err "invalid state payload (size=${BODY_SIZE:-0}, content_length=${CONTENT_LENGTH:-unset})"
        rm -f "$TMP_BODY"
        exit 0
      fi

      STATE_BAK="${STATE_PATH}.bak-ui-$(date +%Y%m%d-%H%M%S)"
      cp "$STATE_PATH" "$STATE_BAK" 2>/dev/null || true
      # Atomic write: stage on the SAME filesystem as the destination so the
      # final mv is a rename(2) — no chance of a truncated JSON if uhttpd
      # kills us at -t 120 mid-write or the box loses power. A direct
      # `cp $TMPFS $OPT` copies chunk-by-chunk across the FS boundary,
      # leaves a half-written file, and the next selfheal `jq` read fails
      # silently → xkeen_bypass empties → every bypass group breaks until
      # the user restores a .bak-ui-*.
      STATE_STAGE="${STATE_PATH}.new-$$"
      if ! cp "$TMP_BODY" "$STATE_STAGE" || ! mv "$STATE_STAGE" "$STATE_PATH"; then
        rm -f "$STATE_STAGE"
        json_err "failed to write state"
        rm -f "$TMP_BODY"
        exit 0
      fi

      json_ok "{\"ok\":true,\"state\":\"$STATE_PATH\"}"
      rm -f "$TMP_BODY"
      exit 0
    fi

    if [ "$KIND" = "outbounds" ]; then
      if ! /opt/bin/jq -e '.outbounds | type == "array" and (map(.tag) | index("vless-reality") != null)' "$TMP_BODY" >/dev/null 2>&1; then
        cp "$TMP_BODY" /tmp/xkeen-outbounds-invalid.json 2>/dev/null || true
        json_err "invalid outbounds payload (size=${BODY_SIZE:-0}, content_length=${CONTENT_LENGTH:-unset})"
        rm -f "$TMP_BODY"
        exit 0
      fi

      OUT_BAK="${OUTBOUNDS_PATH}.bak-ui-$(date +%Y%m%d-%H%M%S)"
      cp "$OUTBOUNDS_PATH" "$OUT_BAK" 2>/dev/null || true
      # Atomic same-FS stage + rename (see state branch above for rationale).
      OUT_STAGE="${OUTBOUNDS_PATH}.new-$$"
      if ! cp "$TMP_BODY" "$OUT_STAGE" || ! mv "$OUT_STAGE" "$OUTBOUNDS_PATH"; then
        rm -f "$OUT_STAGE"
        json_err "failed to write outbounds"
        rm -f "$TMP_BODY"
        exit 0
      fi

      json_ok "{\"ok\":true,\"outbounds\":\"$OUTBOUNDS_PATH\"}"
      rm -f "$TMP_BODY"
      exit 0
    fi

    if [ "$KIND" = "probe" ]; then
      ADDRESS="$(sed -n 's/.*"address"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$TMP_BODY" | head -n 1)"
      PORT="$(sed -n 's/.*"port"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$TMP_BODY" | head -n 1)"
      if [ -z "$ADDRESS" ] || [ -z "$PORT" ] || ! valid_probe_address "$ADDRESS" || ! valid_probe_port "$PORT"; then
        json_err "invalid probe payload"
        rm -f "$TMP_BODY"
        exit 0
      fi

      RESOLVED_IP="$(nslookup "$ADDRESS" 2>/dev/null | awk '/^Address [0-9]*: /{print $3} /^Address: /{print $2}' | tail -n 1)"
      if printf '' | /opt/bin/nc "$ADDRESS" "$PORT" >/dev/null 2>&1; then
        json_ok "{\"ok\":true,\"address\":\"$ADDRESS\",\"port\":$PORT,\"resolvedIp\":\"$RESOLVED_IP\"}"
      else
        json_err "tcp connect failed"
      fi
      rm -f "$TMP_BODY"
      exit 0
    fi

    if [ "$KIND" = "repair-runtime" ]; then
      if repair_runtime; then
        json_ok '{"ok":true,"message":"runtime restored"}'
      else
        json_err "failed to restore xkeen/xray runtime"
      fi
      rm -f "$TMP_BODY"
      exit 0
    fi

    if [ "$KIND" = "subscription-fetch" ]; then
      fetch_subscription
    fi

    if [ "$KIND" = "autoselect-catalog" ]; then
      if ! /opt/bin/jq -e '(.version == 1) and (.entries | type == "array") and all(.entries[]?; (.id|type=="string") and (.profileId|type=="string") and (.address|type=="string") and (.port|type=="number") and (.outbounds|type=="object") and (.singbox|type=="object"))' "$TMP_BODY" >/dev/null 2>&1; then
        json_err "invalid autoselect catalog"
        rm -f "$TMP_BODY"
        exit 0
      fi
      CAT_STAGE="${AUTOSELECT_CATALOG_PATH}.new-$$"
      if ! cp "$TMP_BODY" "$CAT_STAGE" || ! mv "$CAT_STAGE" "$AUTOSELECT_CATALOG_PATH"; then
        rm -f "$CAT_STAGE"
        json_err "failed to write autoselect catalog"
        rm -f "$TMP_BODY"
        exit 0
      fi
      chmod 600 "$AUTOSELECT_CATALOG_PATH" 2>/dev/null || true
      # Restart only after the catalog rename is complete. This makes interval
      # changes effective immediately and guarantees the first loop iteration
      # sees the same server list/configs the UI just saved.
      if [ -x /opt/etc/init.d/S25antigoblin-autoselect ]; then
        /opt/etc/init.d/S25antigoblin-autoselect restart >/dev/null 2>&1 || true
      fi
      json_ok "{\"ok\":true,\"catalog\":\"$AUTOSELECT_CATALOG_PATH\"}"
      rm -f "$TMP_BODY"
      exit 0
    fi

    if [ "$KIND" = "autoselect-run" ]; then
      if [ ! -x "$AUTOSELECT_SCRIPT" ]; then
        json_err "autoselect watchdog is not installed"
        rm -f "$TMP_BODY"
        exit 0
      fi
      # Manual run intentionally returns immediately. The UI polls
      # kind=autoselect-status while the endpoint probes run in background.
      ( exec >/dev/null 2>&1 </dev/null; "$AUTOSELECT_SCRIPT" --force ) &
      json_ok '{"ok":true,"started":true}'
      rm -f "$TMP_BODY"
      exit 0
    fi

    if [ "$KIND" = "singbox" ]; then
      if ! /opt/bin/jq -e '.outbounds | type == "array"' "$TMP_BODY" >/dev/null 2>&1 \
         || ! /opt/bin/jq -e '.inbounds  | type == "array"' "$TMP_BODY" >/dev/null 2>&1; then
        cp "$TMP_BODY" /tmp/xkeen-singbox-invalid.json 2>/dev/null || true
        json_err "invalid singbox payload (size=${BODY_SIZE:-0})"
        rm -f "$TMP_BODY"
        exit 0
      fi

      if [ -x /opt/sbin/sing-box ]; then
        SB_CHECK_LOG="/tmp/xkeen-singbox-check-$$.log"
        if ! validate_singbox_file "$TMP_BODY" "$SB_CHECK_LOG"; then
          # sing-box diagnostics can contain JSON snippets (quotes and
          # backslashes). json_err prints into a JSON string, so flatten and
          # neutralise those two characters before returning the message.
          SB_CHECK_ERR="$(tail -n 4 "$SB_CHECK_LOG" 2>/dev/null | tr '\n' ' ' | tr '"\\' "'/" | cut -c1-320)"
          cp "$TMP_BODY" /tmp/xkeen-singbox-invalid.json 2>/dev/null || true
          rm -f "$SB_CHECK_LOG"
          json_err "sing-box config check failed: $SB_CHECK_ERR"
          rm -f "$TMP_BODY"
          exit 0
        fi
        rm -f "$SB_CHECK_LOG"
      fi

      SINGBOX_PATH="/opt/etc/sing-box/xkeen.json"
      SB_BAK="${SINGBOX_PATH}.bak-ui-$(date +%Y%m%d-%H%M%S)"
      cp "$SINGBOX_PATH" "$SB_BAK" 2>/dev/null || true
      # Atomic same-FS stage + rename (see state branch above).
      SB_STAGE="${SINGBOX_PATH}.new-$$"
      if ! cp "$TMP_BODY" "$SB_STAGE" || ! mv "$SB_STAGE" "$SINGBOX_PATH"; then
        rm -f "$SB_STAGE"
        json_err "failed to write sing-box config"
        rm -f "$TMP_BODY"
        exit 0
      fi

      if ! restart_singbox; then
        if [ -f "$SB_BAK" ]; then
          cp "$SB_BAK" "$SINGBOX_PATH" 2>/dev/null || true
          restart_singbox >/dev/null 2>&1 || true
        fi
        json_err "singbox restart timed out; previous config restored"
        rm -f "$TMP_BODY"
        exit 0
      fi

      json_ok "{\"ok\":true,\"singbox\":\"$SINGBOX_PATH\",\"restarted\":true}"
      rm -f "$TMP_BODY"
      exit 0
    fi

    if [ "$KIND" = "restart-svc" ]; then
      restart_service
    fi

    if ! grep -q '"routing"' "$TMP_BODY" || ! grep -q '"rules"' "$TMP_BODY"; then
      cp "$TMP_BODY" /tmp/xkeen-routing-invalid.json 2>/dev/null || true
      json_err "invalid routing json (size=${BODY_SIZE:-0}, content_length=${CONTENT_LENGTH:-unset})"
      rm -f "$TMP_BODY"
      exit 0
    fi

    TS="$(date +%Y%m%d-%H%M%S)"
    BACKUP="${ROUTING_PATH}.bak-ui-${TS}"
    cp "$ROUTING_PATH" "$BACKUP" 2>/dev/null || true
    # Atomic same-FS stage + rename. TMP_BODY lives on tmpfs (/tmp); a direct
    # `cp` to /opt would leave a truncated file if uhttpd kills us at -t 120
    # mid-copy — xray then boots on the next restart with a corrupt routing
    # config, selfheal watches it fail, and backoff kicks in for 1800s. The
    # same rationale for state / outbounds / singbox writes above.
    ROUTING_STAGE="${ROUTING_PATH}.new-$$"
    if ! cp "$TMP_BODY" "$ROUTING_STAGE" || ! mv "$ROUTING_STAGE" "$ROUTING_PATH"; then
      rm -f "$ROUTING_STAGE"
      json_err "failed to write routing"
      rm -f "$TMP_BODY"
      exit 0
    fi
    # rollback_routing: restore from $BACKUP atomically. Same-FS mv again.
    rollback_routing() {
      [ -f "$BACKUP" ] || return 0
      ROLLBACK_STAGE="${ROUTING_PATH}.rollback-$$"
      if cp "$BACKUP" "$ROLLBACK_STAGE" && mv "$ROLLBACK_STAGE" "$ROUTING_PATH"; then
        return 0
      fi
      rm -f "$ROLLBACK_STAGE"
      return 1
    }
    if validate_confdir; then
      if restart_xray; then
        # Rebuild the bypass / udp-route ipsets synchronously here, using
        # the domains/CIDRs from the just-saved state. The DNS cache
        # (/tmp/xkeen-dns-cache/) makes this cheap on subsequent applies
        # — first-run only pays for uncached domain lookups.
        #
        # Why not xkeen_repair_hooks (the full runtime rebuild)? Because
        # it ALSO calls restart_xray a second time. In practice we saw
        # apply take 60s+ (double restart + cold DNS cache), UI aborts
        # at its 20s fetch timeout, and xray keeps restarting under it.
        #
        # Why not rely purely on the 15s selfheal tick to do this? Because
        # if selfheal-loop hangs for any reason (we've seen ~9h stalls),
        # a user who just added a new bypass domain sees traffic still go
        # through the VPN indefinitely. Doing it inline in apply gives
        # the correct semantics regardless of selfheal health.
        # Return "success" to the UI FIRST, then run the ipset rebuild in
        # a detached child. Rationale: full rebuild for a profile with
        # many bypass domains + many routed domains can hit 30s+ even with
        # parallel resolve, and the browser aborts the fetch at its 45s
        # timeout — the user sees "signal is aborted" although xray IS
        # restarted and routing.json IS live. Since bypass/udp ipset drift
        # is self-correcting on the next selfheal tick (15s), giving the
        # UI the ack up-front and completing the rebuild asynchronously is
        # strictly better UX. The user can still verify "bypass took
        # effect" via a follow-up check.
        (
          # Detach cleanly so uhttpd doesn't block on our stdio.
          exec >/dev/null 2>&1 </dev/null
          if type xkeen_build_bypass_ipset >/dev/null 2>&1; then
            xkeen_ensure_mark || true
            xkeen_build_bypass_ipset || true
            # udp_route rebuild only when the active profile actually needs
            # UDP routing — skips a ~10-20s scan when the profile has no
            # VPN groups at all (typical single-bypass-group setups).
            if xkeen_udp_config_enabled 2>/dev/null; then
              xkeen_build_udp_route_ipset || true
              xkeen_apply_udp_route || true
            else
              xkeen_cleanup_udp_route 2>/dev/null || true
            fi
          fi
        ) &
        json_ok "{\"ok\":true,\"backup\":\"$BACKUP\",\"restarted\":true,\"ipsetRebuildAsync\":true}"
      else
        rollback_routing
        json_err "xray restart failed, rollback applied"
      fi
    else
      rollback_routing
      json_err "xray config validation failed, rollback applied"
    fi

    rm -f "$TMP_BODY"
    exit 0
    ;;
  *)
    printf 'Status: 405 Method Not Allowed\r\n'
    printf 'Content-Type: application/json; charset=utf-8\r\n'
    printf '\r\n'
    printf '{"ok":false,"error":"method not allowed"}\n'
    exit 0
    ;;
esac
