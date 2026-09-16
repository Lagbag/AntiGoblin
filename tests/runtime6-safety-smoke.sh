#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
APP="$ROOT/ui/xkeen-manager/app.js"
HTML="$ROOT/ui/xkeen-manager/index.html"
ROUTING="$ROOT/ui/xkeen-manager/backend/routing.cgi"
AUTO="$ROOT/ui/xkeen-manager/backend/xkeen-autoselect.sh"

need() {
  file="$1" pattern="$2" label="$3"
  grep -qF "$pattern" "$file" || { echo "FAIL: $label" >&2; exit 1; }
}

need "$HTML" 'id="dirtyChip"' 'dirty state chip missing'
need "$HTML" 'id="copyDiagnosticBtn"' 'diagnostic copy button missing'
need "$APP" 'function setRuntimeDirty(value)' 'dirty-state runtime helper missing'
need "$APP" 'function buildDiagnosticReport()' 'diagnostic report builder missing'
need "$APP" 'runtimeVersionStale' 'applied runtime version mismatch detection missing'
need "$APP" 'failureReasonLabel' 'auto-select quarantine UI missing'
need "$ROUTING" 'appliedMeta:' 'health does not expose applied metadata'
need "$ROUTING" 'probe_https_via proxy' 'apply does not use neutral HTTPS proof'
need "$ROUTING" ': > /tmp/antigoblin-singbox-runtime.log' 'sing-box diagnostic log is not scoped to fresh runtime'
need "$AUTO" 'FAILURE_CACHE_PATH=' 'auto-select failure quarantine missing'
need "$AUTO" 'cooldownUntil' 'auto-select status does not expose cooldown'
need "$AUTO" 'startup_failures' 'boot-time short retry logic missing'
need "$AUTO" 'state commit failed; rolling runtime back' 'auto-select state/runtime rollback missing'

node --check "$APP" >/dev/null
sh -n "$ROUTING"
sh -n "$AUTO"
echo 'PASS: runtime6 safety/UX invariants'
