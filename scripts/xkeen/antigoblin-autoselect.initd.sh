#!/bin/sh
PATH=/opt/bin:/opt/sbin:/sbin:/bin:/usr/sbin:/usr/bin
SCRIPT=/opt/share/xkeen-manager/api/xkeen-autoselect.sh
PIDFILE=/opt/var/run/antigoblin-autoselect.pid
LOG=/opt/var/log/antigoblin-autoselect.log

is_running() {
  [ -f "$PIDFILE" ] || return 1
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  kill -0 "$pid" 2>/dev/null || return 1
  cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  case "$cmd" in *xkeen-autoselect.sh*--loop*) return 0 ;; esac
  return 1
}

start() {
  mkdir -p /opt/var/run /opt/var/log 2>/dev/null || true
  if is_running; then echo "antigoblin autoselect already running"; return 0; fi
  rm -f "$PIDFILE"
  [ -x "$SCRIPT" ] || { echo "missing $SCRIPT" >&2; return 1; }
  /opt/sbin/start-stop-daemon -S -b -m -p "$PIDFILE" -x /opt/bin/sh -- "$SCRIPT" --loop >>"$LOG" 2>&1
}

stop() {
  if [ -f "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    case "$pid" in ''|*[!0-9]*) ;; *) kill "$pid" 2>/dev/null || true ;; esac
  fi
  pkill -f 'xkeen-autoselect.sh --loop' 2>/dev/null || true
  rm -f "$PIDFILE"
}

case "$1" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status) if is_running; then echo "antigoblin autoselect running"; else echo "antigoblin autoselect stopped"; exit 1; fi ;;
  *) echo "Usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac
