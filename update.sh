#!/bin/sh
# AntiGoblin in-place updater. Preserves user state/configs and lets install.sh
# create a pre-upgrade backup before replacing UI/backend/runtime scripts.
set -eu

PATH=/opt/sbin:/opt/bin:/opt/usr/sbin:/opt/usr/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

REPO_OWNER="${ANTIGOBLIN_REPO_OWNER:-Lagbag}"
REPO_NAME="${ANTIGOBLIN_REPO_NAME:-AntiGoblin}"
REPO_BRANCH="${ANTIGOBLIN_REPO_BRANCH:-main}"
INSTALL_URL="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${REPO_BRANCH}/install.sh"

case "${1:-}" in
  --help|-h)
    cat <<'EOF'
Usage:
  sh update.sh          upgrade AntiGoblin in place
  sh update.sh --force  upgrade and reseed safe sample configs

State, keys and generated routing/outbound configs are preserved.
EOF
    exit 0
    ;;
esac
TMP="/opt/tmp/antigoblin-update-install.sh"

[ -d /opt ] || { echo "ERROR: /opt is not mounted; Entware is required." >&2; exit 1; }
mkdir -p /opt/tmp

if [ -x /opt/bin/curl ]; then
  CURL=/opt/bin/curl
elif command -v curl >/dev/null 2>&1; then
  CURL="$(command -v curl)"
elif [ -x /opt/bin/opkg ]; then
  echo "==> Installing curl for HTTPS download"
  /opt/bin/opkg update >/dev/null 2>&1 || true
  /opt/bin/opkg install curl >/dev/null 2>&1 || true
  CURL=/opt/bin/curl
else
  echo "ERROR: curl is unavailable. Install it with: /opt/bin/opkg install curl" >&2
  exit 1
fi

[ -x "$CURL" ] || { echo "ERROR: curl installation failed." >&2; exit 1; }
echo "==> Downloading current AntiGoblin installer from ${REPO_OWNER}/${REPO_NAME}:${REPO_BRANCH}"
"$CURL" -fsSL -o "$TMP" "$INSTALL_URL"
chmod 700 "$TMP" 2>/dev/null || true
exec /opt/bin/sh "$TMP" --update "$@"
