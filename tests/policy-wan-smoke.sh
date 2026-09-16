#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
TMP="${TMPDIR:-/tmp}/antigoblin-policy-test-$$"
mkdir -p "$TMP"
trap 'rm -rf "$TMP"' EXIT INT TERM

# Source the real runtime functions while replacing router-only /opt/bin/awk
# with the host awk so this regression test also runs on CI/dev machines.
sed 's#/opt/bin/awk#awk#g' "$ROOT/ui/xkeen-manager/backend/xkeen-runtime.sh" > "$TMP/runtime.sh"
cat > "$TMP/ndmc" <<'NDMC'
#!/bin/sh
cat "$NDMC_FIXTURE"
NDMC
chmod +x "$TMP/ndmc"
PATH="$TMP:$PATH"
export PATH
. "$TMP/runtime.sh"

check_yes() {
  fixture="$1"
  printf '%s\n' "$2" > "$fixture"
  NDMC_FIXTURE="$fixture"; export NDMC_FIXTURE
  xkeen_policy_has_wan Policy42 ISP || {
    echo "FAIL: expected Policy42/ISP to be permitted" >&2
    exit 1
  }
}

check_no() {
  fixture="$1"
  printf '%s\n' "$2" > "$fixture"
  NDMC_FIXTURE="$fixture"; export NDMC_FIXTURE
  if xkeen_policy_has_wan Policy42 ISP; then
    echo "FAIL: expected Policy42/ISP to be missing/denied" >&2
    exit 1
  fi
}

check_yes "$TMP/block.cfg" 'ip policy Policy42
 description xkeen
 permit global ISP
!
ip policy Policy0
 permit global ISP
!'

check_yes "$TMP/flat.cfg" 'ip policy Policy42 permit global ISP
ip policy Policy0 permit global ISP'

check_no "$TMP/missing.cfg" 'ip policy Policy42
 description xkeen
!
ip policy Policy0
 permit global ISP
!'

check_no "$TMP/denied.cfg" 'ip policy Policy42
 permit global ISP
 no permit global ISP
!'

echo 'PASS: xkeen policy WAN parser handles block/flat/missing/denied configs'
