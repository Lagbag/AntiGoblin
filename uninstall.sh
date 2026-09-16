#!/bin/sh
# AntiGoblin uninstaller.
# Default: remove runtime/UI but keep xray/sing-box configs and a state backup.
# --purge: additionally remove AntiGoblin xray/sing-box configs and UI state.
set -eu

PATH=/opt/sbin:/opt/bin:/opt/usr/sbin:/opt/usr/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
PURGE=0

usage() {
  cat <<'EOF'
Usage:
  sh uninstall.sh          remove AntiGoblin runtime/UI, keep VPN configs + backup
  sh uninstall.sh --purge  also remove AntiGoblin xray/sing-box generated configs

The Keenetic policy named/described "xkeen" and /opt/sbin/sing-box are not
removed automatically because they may be referenced by other configuration.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

backup_state() {
  stamp="$(date '+%Y%m%d-%H%M%S' 2>/dev/null || echo now)"
  dst="/opt/var/backups/antigoblin/uninstall-$stamp"
  mkdir -p "$dst/xray" "$dst/sing-box"
  [ -f /opt/share/xkeen-manager/xkeen-ui-state.json ] && cp /opt/share/xkeen-manager/xkeen-ui-state.json "$dst/xkeen-ui-state.json" || true
  [ -f /opt/etc/antigoblin.conf ] && cp /opt/etc/antigoblin.conf "$dst/antigoblin.conf" || true
  [ -f /opt/share/xkeen-manager/VERSION ] && cp /opt/share/xkeen-manager/VERSION "$dst/VERSION" || true
  for f in /opt/etc/xray/configs/*.json; do [ -f "$f" ] && cp "$f" "$dst/xray/$(basename "$f")" || true; done
  for f in /opt/etc/sing-box/*.json; do [ -f "$f" ] && cp "$f" "$dst/sing-box/$(basename "$f")" || true; done
  echo "$dst"
}

if [ ! -d /opt ]; then
  echo "ERROR: /opt is not mounted." >&2
  exit 1
fi

BACKUP_DIR="$(backup_state)"
echo "==> Backup: $BACKUP_DIR"

echo "==> Stopping AntiGoblin services"
for svc in /opt/etc/init.d/S26antigoblin /opt/etc/init.d/S25antigoblin-selfheal /opt/etc/init.d/S24antigoblin-singbox; do
  [ -x "$svc" ] && "$svc" stop >/dev/null 2>&1 || true
done
pkill -f 'xkeen-selfheal-loop.sh' 2>/dev/null || true

# Resolve the Keenetic policy mark used by AntiGoblin, if available.
MARK_HEX="$(cat /tmp/xkeen-mark 2>/dev/null || true)"
case "$MARK_HEX" in
  ''|*[!0-9a-fA-F]*) MARK_HEX="" ;;
esac

# Remove jumps/chains repeatedly in case an older install duplicated hooks.
# If /tmp/xkeen-mark disappeared, delete jumps by their unique target-chain name
# instead of relying on an exact rule specification.
delete_jumps_to_target() {
  bin="$1"
  table="$2"
  chain="$3"
  target="$4"
  command -v "$bin" >/dev/null 2>&1 || return 0
  while :; do
    rule_no="$($bin -t "$table" -L "$chain" --line-numbers -n 2>/dev/null | awk -v target="$target" '$2 == target { print $1; exit }')"
    [ -n "$rule_no" ] || break
    $bin -t "$table" -D "$chain" "$rule_no" >/dev/null 2>&1 || break
  done
}

echo "==> Removing netfilter/ipset runtime"
if [ -n "$MARK_HEX" ]; then
  while iptables -t nat -D PREROUTING -m connmark --mark "0x$MARK_HEX" -m conntrack ! --ctstate INVALID -j xkeen 2>/dev/null; do :; done
  while iptables -t mangle -D PREROUTING -m connmark --mark "0x$MARK_HEX" -m conntrack ! --ctstate INVALID -p udp -m set --match-set xkeen_udp_route dst -j xkeen_udp_route 2>/dev/null; do :; done
  if command -v ip6tables >/dev/null 2>&1; then
    while ip6tables -D FORWARD -m connmark --mark "0x$MARK_HEX" -j REJECT --reject-with icmp6-port-unreachable 2>/dev/null; do :; done
  fi
fi
# Always sweep uniquely named target chains too: this also cleans duplicated hooks
# left by much older builds and works when the cached mark is missing.
delete_jumps_to_target iptables nat PREROUTING xkeen
delete_jumps_to_target iptables mangle PREROUTING xkeen_udp_route
iptables -t nat -F xkeen 2>/dev/null || true
iptables -t nat -X xkeen 2>/dev/null || true
iptables -t mangle -F xkeen_udp_route 2>/dev/null || true
iptables -t mangle -X xkeen_udp_route 2>/dev/null || true
ipset destroy xkeen_udp_route 2>/dev/null || true
ipset destroy xkeen_bypass 2>/dev/null || true
while ip rule del fwmark 0x111/0x111 table 111 2>/dev/null; do :; done
while ip rule del fwmark 0x111 table 111 2>/dev/null; do :; done
ip route flush table 111 2>/dev/null || true

echo "==> Removing init scripts, hooks, cron and UI"
rm -f /opt/etc/init.d/S20antigoblin-sysctl \
      /opt/etc/init.d/S24antigoblin-singbox \
      /opt/etc/init.d/S25antigoblin-selfheal \
      /opt/etc/init.d/S26antigoblin
rm -f /opt/etc/cron.1min/50-antigoblin-selfheal
rm -f /opt/etc/ndm/usb.d/50-antigoblin.sh \
      /opt/etc/ndm/netfilter.d/50-antigoblin.sh \
      /opt/etc/ndm/fs.d/50-antigoblin.sh
rm -f /opt/etc/antigoblin.conf /opt/etc/antigoblin.done
rm -rf /opt/share/xkeen-manager
rm -f /opt/var/run/antigoblin-selfheal-loop.pid /tmp/xkeen-mark /tmp/xkeen-needs-xray-restart
rm -f /opt/var/log/xkeen-*.log /opt/var/log/sing-box-xkeen.log /opt/var/log/xkeen-manager-uhttpd.log

if [ "$PURGE" = "1" ]; then
  echo "==> Purging AntiGoblin VPN configs"
  rm -f /opt/etc/xray/configs/01_log.json \
        /opt/etc/xray/configs/02_relay.json \
        /opt/etc/xray/configs/03_inbounds.json \
        /opt/etc/xray/configs/04_outbounds.json \
        /opt/etc/xray/configs/05_routing.json
  rm -f /opt/etc/sing-box/xkeen.json
fi

cat <<EOF

AntiGoblin removed.
Backup kept at: $BACKUP_DIR

Not removed automatically:
  - Keenetic policy "xkeen" (remove it in Keenetic UI if you no longer need it)
  - /opt/sbin/sing-box (may be shared with other setups)
EOF
