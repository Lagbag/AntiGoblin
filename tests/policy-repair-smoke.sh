#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
TMP="${TMPDIR:-/tmp}/ag-policy-smoke-$$"
mkdir -p "$TMP"
trap 'rm -rf "$TMP"' EXIT INT TERM

# xkeen-runtime.sh expects Entware awk on-router. Replace it only inside this
# host-side smoke test so we can exercise the actual policy repair function.
sed 's#/opt/bin/awk#awk#g' "$ROOT/ui/xkeen-manager/backend/xkeen-runtime.sh" > "$TMP/runtime.sh"
. "$TMP/runtime.sh"

policy_has_wan=0
permit_called=0

ndmc() {
  [ "${1:-}" = "-c" ] || return 1
  cmd="${2:-}"
  case "$cmd" in
    'show interface')
      cat <<'OUT'
Interface, name = ISP
  defaultgw: yes
OUT
      ;;
    'show ip policy')
      echo 'policy, name = Policy42, description = xkeen, mark = 0x2a, multipath = no'
      ;;
    'show running-config')
      echo 'ip policy Policy42'
      echo '    description xkeen'
      if [ "$policy_has_wan" = "1" ]; then
        echo '    permit global ISP'
      fi
      echo '!'
      ;;
    'ip policy Policy42 permit global ISP')
      policy_has_wan=1
      permit_called=1
      ;;
    'system configuration save') : ;;
    *) echo "unexpected ndmc command: $cmd" >&2; return 1 ;;
  esac
}

sleep() { :; }

[ "$(xkeen_policy_name)" = "Policy42" ]
! xkeen_policy_has_wan Policy42 ISP
xkeen_ensure_policy
[ "$permit_called" = "1" ]
[ "$policy_has_wan" = "1" ]
xkeen_policy_has_wan Policy42 ISP

echo 'PASS: existing xkeen policy without WAN is repaired in place'
