#!/bin/sh
# AntiGoblin latency watchdog / automatic proxy selector.
# Reads browser-generated per-proxy runtime configs from autoselect-catalog.json,
# probes every endpoint without holding the global apply lock, then switches the
# active proxy atomically under that lock. Probe results live in /tmp to avoid
# periodic flash writes.

PATH="/opt/bin:/opt/sbin:/sbin:/usr/sbin:/bin:/usr/bin:$PATH"
STATE_PATH="/opt/share/xkeen-manager/xkeen-ui-state.json"
CATALOG_PATH="/opt/share/xkeen-manager/autoselect-catalog.json"
STATUS_PATH="/tmp/antigoblin-autoselect-status.json"
RUNTIME_LIB="/opt/share/xkeen-manager/api/xkeen-runtime.sh"
OUTBOUNDS_PATH="/opt/etc/xray/configs/04_outbounds.json"
SINGBOX_PATH="/opt/etc/sing-box/xkeen.json"
XRAY_BIN="/opt/sbin/xray"
SINGBOX_BIN="/opt/sbin/sing-box"
LOG_PATH="/opt/var/log/antigoblin-autoselect.log"
MAX_PARALLEL=6

[ -r "$RUNTIME_LIB" ] && . "$RUNTIME_LIB"

log() {
  mkdir -p /opt/var/log 2>/dev/null || true
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || date)" "$*" >> "$LOG_PATH" 2>/dev/null || true
}

json_status() {
  # args: phase message switched best_id best_ms current_id current_ms results_file
  phase="$1"; message="$2"; switched="$3"; best_id="$4"; best_ms="$5"; current_id="$6"; current_ms="$7"; results_file="$8"
  now="$(date +%s 2>/dev/null || echo 0)"
  case "$best_ms" in ''|null|*[!0-9.]*) best_json=null ;; *) best_json="$best_ms" ;; esac
  case "$current_ms" in ''|null|*[!0-9.]*) current_json=null ;; *) current_json="$current_ms" ;; esac
  if [ -f "$results_file" ]; then
    /opt/bin/jq -n \
      --arg phase "$phase" --arg message "$message" --argjson switched "$switched" \
      --arg bestId "$best_id" --argjson bestMs "$best_json" \
      --arg currentId "$current_id" --argjson currentMs "$current_json" \
      --argjson updatedAt "$now" --slurpfile results "$results_file" \
      '{ok:true,phase:$phase,message:$message,switched:$switched,bestId:$bestId,bestLatencyMs:$bestMs,currentId:$currentId,currentLatencyMs:$currentMs,updatedAt:$updatedAt,results:($results[0] // [])}' \
      > "${STATUS_PATH}.new" 2>/dev/null || return 1
  else
    /opt/bin/jq -n \
      --arg phase "$phase" --arg message "$message" --argjson switched "$switched" \
      --arg bestId "$best_id" --argjson bestMs "$best_json" \
      --arg currentId "$current_id" --argjson currentMs "$current_json" \
      --argjson updatedAt "$now" \
      '{ok:true,phase:$phase,message:$message,switched:$switched,bestId:$bestId,bestLatencyMs:$bestMs,currentId:$currentId,currentLatencyMs:$currentMs,updatedAt:$updatedAt,results:[]}' \
      > "${STATUS_PATH}.new" 2>/dev/null || return 1
  fi
  mv "${STATUS_PATH}.new" "$STATUS_PATH" 2>/dev/null || true
}

active_profile_json() {
  /opt/bin/jq -c '
    (.activeProfileId // "") as $id
    | (.profiles[]? | select(.id == $id)) // .profiles[0] // empty
  ' "$STATE_PATH" 2>/dev/null
}

cfg_value() {
  key="$1" fallback="$2"
  active_profile_json | /opt/bin/jq -r --arg k "$key" --arg fb "$fallback" '.autoSelect[$k] // $fb' 2>/dev/null
}

is_enabled() {
  [ "$(cfg_value enabled true)" = "true" ]
}

interval_sec() {
  n="$(cfg_value intervalSec 300)"
  case "$n" in ''|*[!0-9]*) n=300 ;; esac
  [ "$n" -lt 60 ] && n=60
  [ "$n" -gt 3600 ] && n=3600
  printf '%s\n' "$n"
}

probe_timeout_sec() {
  n="$(cfg_value probeTimeoutSec 2)"
  case "$n" in ''|*[!0-9]*) n=2 ;; esac
  [ "$n" -lt 1 ] && n=1
  [ "$n" -gt 5 ] && n=5
  printf '%s\n' "$n"
}

min_improvement_ms() {
  n="$(cfg_value minImprovementMs 0)"
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  [ "$n" -gt 500 ] && n=500
  printf '%s\n' "$n"
}

safe_telnet_url() {
  host="$1" port="$2"
  case "$host" in *:*) printf 'telnet://[%s]:%s' "$host" "$port" ;; *) printf 'telnet://%s:%s' "$host" "$port" ;; esac
}

probe_one() {
  id="$1" name="$2" address="$3" port="$4" protocol="$5" timeout="$6" outfile="$7"
  latency=""
  method=""
  error=""

  case "$protocol" in
    hysteria|hysteria2|tuic|wireguard) udp_only=1 ;;
    *) udp_only=0 ;;
  esac

  # For TCP-based proxy protocols, verify the actual service port and rank
  # by TCP connect time. A host can answer ICMP while its proxy port is dead;
  # selecting it purely by ping would black-hole traffic after the switch.
  if [ "$udp_only" = "0" ] && [ -x /opt/bin/curl ]; then
    URL="$(safe_telnet_url "$address" "$port")"
    CONNECT_S="$(/opt/bin/curl -sS --connect-timeout "$timeout" --max-time "$timeout" -o /dev/null -w '%{time_connect}' "$URL" 2>/dev/null || true)"
    if printf '%s' "$CONNECT_S" | grep -Eq '^[0-9]+([.][0-9]+)?$' \
        && /opt/bin/awk -v n="$CONNECT_S" 'BEGIN { exit !(n > 0) }'; then
      latency="$(/opt/bin/awk -v n="$CONNECT_S" 'BEGIN { printf "%d", (n*1000)+0.5 }')"
      method="tcp"
    else
      error="tcp connect failed"
    fi
  else
    # QUIC/UDP transports have no protocol-neutral handshake that works for
    # Hysteria/TUIC/WireGuard alike. Use ICMP RTT for ranking; the post-switch
    # SOCKS egress verification below catches a dead remote service.
    PING_OUT="$(ping -c 1 -W "$timeout" "$address" 2>/dev/null || true)"
    latency="$(printf '%s\n' "$PING_OUT" | sed -n 's/.*time[=<]\([0-9.][0-9.]*\)[[:space:]]*ms.*/\1/p' | tail -n 1)"
    if [ -n "$latency" ]; then
      latency="$(/opt/bin/awk -v n="$latency" 'BEGIN { printf "%d", (n+0.5) }')"
      method="icmp"
    else
      error="timeout / ICMP blocked"
    fi
  fi

  if [ -n "$latency" ]; then
    /opt/bin/jq -n --arg id "$id" --arg name "$name" --arg addr "$address" \
      --arg proto "$protocol" --arg method "$method" --argjson port "$port" \
      --argjson ms "$latency" \
      '{id:$id,name:$name,address:$addr,port:$port,protocol:$proto,ok:true,latencyMs:$ms,method:$method}' > "$outfile"
  else
    /opt/bin/jq -n --arg id "$id" --arg name "$name" --arg addr "$address" \
      --arg proto "$protocol" --arg err "${error:-unreachable}" --argjson port "$port" \
      '{id:$id,name:$name,address:$addr,port:$port,protocol:$proto,ok:false,latencyMs:null,method:"",error:$err}' > "$outfile"
  fi
}

validate_cmd_bounded() {
  # $1 timeout seconds, remaining args command
  limit="$1"; shift
  "$@" >/dev/null 2>&1 &
  vpid=$!
  vi=0
  while [ "$vi" -lt "$limit" ] && kill -0 "$vpid" 2>/dev/null; do
    sleep 1
    vi=$((vi + 1))
  done
  if kill -0 "$vpid" 2>/dev/null; then
    kill "$vpid" 2>/dev/null || true
    sleep 1
    kill -9 "$vpid" 2>/dev/null || true
    wait "$vpid" 2>/dev/null || true
    return 124
  fi
  wait "$vpid"
}

restart_singbox_bounded() {
  killall sing-box 2>/dev/null || true
  i=0
  while [ $i -lt 6 ] && pidof sing-box >/dev/null 2>&1; do sleep 1; i=$((i+1)); done
  if pidof sing-box >/dev/null 2>&1; then killall -9 sing-box 2>/dev/null || true; sleep 1; fi
  rm -f /opt/var/run/sing-box.pid 2>/dev/null || true
  /opt/sbin/start-stop-daemon -S -b -m -p /opt/var/run/sing-box.pid -x "$SINGBOX_BIN" -- run -c "$SINGBOX_PATH" >>/opt/var/log/sing-box-xkeen.log 2>&1 || return 1
  i=0
  while [ $i -lt 10 ]; do
    if pidof sing-box >/dev/null 2>&1 && netstat -lnpu 2>/dev/null | grep -q ':61221 '; then
      if /opt/bin/jq -e '.inbounds[]? | select(.listen_port == 61225)' "$SINGBOX_PATH" >/dev/null 2>&1; then
        netstat -lnpt 2>/dev/null | grep -q ':61225 ' || { sleep 1; i=$((i+1)); continue; }
      fi
      return 0
    fi
    sleep 1; i=$((i+1))
  done
  return 1
}

restart_xray_bounded() {
  OLD="$(pidof xray 2>/dev/null | /opt/bin/awk '{print $1}')"
  killall xray 2>/dev/null || true
  i=0
  while [ $i -lt 6 ] && [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; do sleep 1; i=$((i+1)); done
  if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
    CMDLINE="$(tr '\0' ' ' < "/proc/$OLD/cmdline" 2>/dev/null || true)"
    case "$CMDLINE" in *xray*) kill -9 "$OLD" 2>/dev/null || true ;; esac
  fi
  rm -f /opt/var/run/xray-ui.pid /opt/var/run/xray.pid 2>/dev/null || true
  XRAY_LOCATION_ASSET=/opt/etc/xray/dat XRAY_LOCATION_CONFDIR=/opt/etc/xray/configs \
    /opt/sbin/start-stop-daemon -S -b -m -p /opt/var/run/xray-ui.pid -x "$XRAY_BIN" -- run >>/opt/var/log/xray-manual.log 2>&1 || return 1
  i=0
  while [ $i -lt 12 ]; do
    netstat -lnpt 2>/dev/null | grep -q ':61219 ' && return 0
    sleep 1; i=$((i+1))
  done
  return 1
}

verify_tunnel_egress() {
  # Syntax/listener checks are not health checks. A QUIC endpoint can answer
  # ICMP while rejecting Hysteria/TUIC credentials. First try a neutral HTTPS
  # page, then public-IP endpoints as fallbacks.
  [ -x /opt/bin/curl ] || return 0
  /opt/bin/curl -4 -fsS -o /dev/null \
    --socks5-hostname 127.0.0.1:61080 \
    --connect-timeout 4 --max-time 8 https://example.com >/dev/null 2>&1 && return 0
  for url in https://api.ipify.org https://ifconfig.me/ip; do
    out="$(/opt/bin/curl -4 -fsS --socks5-hostname 127.0.0.1:61080 --connect-timeout 4 --max-time 8 "$url" 2>/dev/null | tr -d '\r\n ' | head -c 80)"
    case "$out" in
      ''|*[!0-9a-fA-F:.]*) ;;
      *) return 0 ;;
    esac
  done
  return 1
}

apply_proxy() {
  target_id="$1" profile_id="$2"
  LOCK_HELD=0
  release_apply_lock() {
    if [ "$LOCK_HELD" = "1" ] && type xkeen_lock_release >/dev/null 2>&1; then
      xkeen_lock_release 2>/dev/null || true
      LOCK_HELD=0
      trap - EXIT INT TERM
    fi
  }
  [ -f "$CATALOG_PATH" ] || return 1
  [ -x "$XRAY_BIN" ] || return 1
  [ -x "$SINGBOX_BIN" ] || return 1

  TMP_OUT="${OUTBOUNDS_PATH}.autoselect-new-$$"
  TMP_SB="${SINGBOX_PATH}.autoselect-new-$$"
  BAK_OUT="${OUTBOUNDS_PATH}.autoselect-bak"
  BAK_SB="${SINGBOX_PATH}.autoselect-bak"

  /opt/bin/jq --arg id "$target_id" --arg pid "$profile_id" -e '.entries[] | select(.id == $id and .profileId == $pid) | .outbounds' "$CATALOG_PATH" > "$TMP_OUT" 2>/dev/null || { rm -f "$TMP_OUT" "$TMP_SB"; return 1; }
  /opt/bin/jq --arg id "$target_id" --arg pid "$profile_id" -e '.entries[] | select(.id == $id and .profileId == $pid) | .singbox' "$CATALOG_PATH" > "$TMP_SB" 2>/dev/null || { rm -f "$TMP_OUT" "$TMP_SB"; return 1; }
  /opt/bin/jq -e '.outbounds | type == "array"' "$TMP_OUT" >/dev/null 2>&1 || { rm -f "$TMP_OUT" "$TMP_SB"; return 1; }
  /opt/bin/jq -e '.outbounds | type == "array"' "$TMP_SB" >/dev/null 2>&1 || { rm -f "$TMP_OUT" "$TMP_SB"; return 1; }
  validate_cmd_bounded 12 "$SINGBOX_BIN" check -c "$TMP_SB" || { log "candidate=$target_id sing-box check failed/timeout"; rm -f "$TMP_OUT" "$TMP_SB"; return 1; }

  # The browser and watchdog share the same lock only for the short commit /
  # restart phase. Endpoint probing above never blocks Save & Apply.
  if type xkeen_lock_acquire >/dev/null 2>&1; then
    xkeen_lock_acquire || { rm -f "$TMP_OUT" "$TMP_SB"; return 2; }
    LOCK_HELD=1
    trap 'xkeen_lock_release 2>/dev/null || true' EXIT INT TERM
  fi

  cp "$OUTBOUNDS_PATH" "$BAK_OUT" 2>/dev/null || true
  cp "$SINGBOX_PATH" "$BAK_SB" 2>/dev/null || true
  if ! mv "$TMP_OUT" "$OUTBOUNDS_PATH"; then
    rm -f "$TMP_SB"
    release_apply_lock
    return 1
  fi
  if ! mv "$TMP_SB" "$SINGBOX_PATH"; then
    log "candidate=$target_id sing-box config commit failed; rollback"
    [ -f "$BAK_OUT" ] && cp "$BAK_OUT" "$OUTBOUNDS_PATH"
    rm -f "$TMP_SB"
    release_apply_lock
    return 1
  fi

  if ! validate_cmd_bounded 12 "$XRAY_BIN" run -test -confdir /opt/etc/xray/configs; then
    log "candidate=$target_id xray validation failed; rollback"
    [ -f "$BAK_OUT" ] && cp "$BAK_OUT" "$OUTBOUNDS_PATH"
    [ -f "$BAK_SB" ] && cp "$BAK_SB" "$SINGBOX_PATH"
    release_apply_lock
    return 1
  fi

  if ! restart_singbox_bounded || ! restart_xray_bounded; then
    log "candidate=$target_id restart failed; rollback"
    [ -f "$BAK_OUT" ] && cp "$BAK_OUT" "$OUTBOUNDS_PATH"
    [ -f "$BAK_SB" ] && cp "$BAK_SB" "$SINGBOX_PATH"
    restart_singbox_bounded >/dev/null 2>&1 || true
    restart_xray_bounded >/dev/null 2>&1 || true
    release_apply_lock
    return 1
  fi

  if ! verify_tunnel_egress; then
    log "candidate=$target_id egress verification failed; rollback"
    [ -f "$BAK_OUT" ] && cp "$BAK_OUT" "$OUTBOUNDS_PATH"
    [ -f "$BAK_SB" ] && cp "$BAK_SB" "$SINGBOX_PATH"
    restart_singbox_bounded >/dev/null 2>&1 || true
    restart_xray_bounded >/dev/null 2>&1 || true
    release_apply_lock
    return 1
  fi

  TMP_STATE="${STATE_PATH}.autoselect-new-$$"
  if /opt/bin/jq --arg pid "$profile_id" --arg id "$target_id" \
      '(.profiles[] | select(.id == $pid) | .activeProxyId) = $id' "$STATE_PATH" > "$TMP_STATE" 2>/dev/null \
      && /opt/bin/jq -e '.' "$TMP_STATE" >/dev/null 2>&1; then
    mv "$TMP_STATE" "$STATE_PATH"
    chmod 600 "$STATE_PATH" 2>/dev/null || true
  else
    rm -f "$TMP_STATE"
    log "candidate=$target_id switched runtime but failed to update state"
  fi

  rm -f "$BAK_OUT" "$BAK_SB" 2>/dev/null || true
  release_apply_lock
  date +%s > /tmp/antigoblin-autoselect-last-switch.ts 2>/dev/null || true
  return 0
}

run_once_inner() {
  force="${1:-0}"
  [ -x /opt/bin/jq ] || return 1
  [ -f "$STATE_PATH" ] || return 1
  [ -f "$CATALOG_PATH" ] || { json_status error "catalog missing; save/apply once" false "" "" "" "" /dev/null; return 1; }

  profile_id="$(active_profile_json | /opt/bin/jq -r '.id // ""' 2>/dev/null)"
  current_id="$(active_profile_json | /opt/bin/jq -r '.activeProxyId // ""' 2>/dev/null)"
  if [ -z "$profile_id" ]; then return 1; fi
  if [ "$force" != "1" ] && ! is_enabled; then return 0; fi

  count="$(/opt/bin/jq --arg pid "$profile_id" '[.entries[]? | select(.profileId == $pid)] | length' "$CATALOG_PATH" 2>/dev/null || echo 0)"
  case "$count" in ''|*[!0-9]*) count=0 ;; esac
  if [ "$count" -eq 0 ]; then
    json_status error "no probeable servers in catalog" false "" "" "$current_id" "" /dev/null
    return 1
  fi

  timeout="$(probe_timeout_sec)"
  tmpdir="/tmp/antigoblin-probes-$$"
  mkdir -p "$tmpdir" || return 1
  json_status probing "probing $count servers" false "" "" "$current_id" "" /dev/null

  n=0
  /opt/bin/jq -r --arg pid "$profile_id" '.entries[]? | select(.profileId == $pid) | [.id,.name,.address,(.port|tostring),.protocol] | @tsv' "$CATALOG_PATH" 2>/dev/null > "$tmpdir/entries.tsv" || true
  while IFS="$(printf '\t')" read -r id name address port protocol; do
      [ -n "$id" ] && [ -n "$address" ] || continue
      case "$port" in ''|*[!0-9]*) continue ;; esac
      probe_one "$id" "$name" "$address" "$port" "$protocol" "$timeout" "$tmpdir/$id.json" &
      n=$((n + 1))
      if [ $((n % MAX_PARALLEL)) -eq 0 ]; then wait; fi
    done < "$tmpdir/entries.tsv"
  wait

  if ls "$tmpdir"/*.json >/dev/null 2>&1; then
    /opt/bin/jq -s 'sort_by([if .ok then .latencyMs else 999999999 end, .name])' "$tmpdir"/*.json > "$tmpdir/results.json" 2>/dev/null || echo '[]' > "$tmpdir/results.json"
  else
    echo '[]' > "$tmpdir/results.json"
  fi

  best_id="$(/opt/bin/jq -r '[.[] | select(.ok == true and (.latencyMs|type == "number"))] | sort_by(.latencyMs) | .[0].id // ""' "$tmpdir/results.json")"
  best_ms="$(/opt/bin/jq -r --arg id "$best_id" '.[] | select(.id == $id) | .latencyMs' "$tmpdir/results.json" | head -n1)"
  current_ms="$(/opt/bin/jq -r --arg id "$current_id" '.[] | select(.id == $id and .ok == true) | .latencyMs' "$tmpdir/results.json" | head -n1)"

  if [ -z "$best_id" ]; then
    json_status error "all servers unreachable by latency probes" false "" "" "$current_id" "$current_ms" "$tmpdir/results.json"
    rm -rf "$tmpdir"
    return 1
  fi

  # runtime2 ranked UDP protocols by ICMP but did not re-validate the already
  # active node when it remained the lowest RTT. That lets a dead HY2/TUIC
  # endpoint stay selected forever. Verify the current tunnel every cycle.
  current_tunnel_ok=1
  if [ -n "$current_id" ]; then
    if verify_tunnel_egress; then
      current_tunnel_ok=1
    else
      current_tunnel_ok=0
      log "current tunnel failed egress validation id=$current_id latency=${current_ms:-unknown}ms"
    fi
  fi

  switch=0
  reason="best server already active"
  if [ -z "$current_id" ] || [ -z "$current_ms" ]; then
    switch=1; reason="current server unreachable"
  elif [ "$current_tunnel_ok" != "1" ]; then
    switch=1; reason="current tunnel failed HTTPS validation"
  elif [ "$best_id" != "$current_id" ]; then
    threshold="$(min_improvement_ms)"
    improvement=$((current_ms - best_ms))
    if [ "$improvement" -ge "$threshold" ]; then
      switch=1; reason="better by ${improvement} ms"
    else
      reason="best is only ${improvement} ms faster (< ${threshold} ms threshold)"
    fi
  fi

  if [ "$switch" = "1" ]; then
    log "switch request current=$current_id/$current_ms best=$best_id/$best_ms reason=$reason"

    # Endpoint ping/TCP-connect only proves that a server answers. The fastest
    # endpoint can still reject credentials or have a broken proxy path. Try
    # candidates in measured-latency order until one also passes the real
    # SOCKS egress verification in apply_proxy(). This prevents the watchdog
    # from repeatedly selecting a dead-but-pingable node forever.
    switched_ok=0
    busy=0
    tried=0
    selected_id=""
    selected_ms=""
    /opt/bin/jq -r '[.[] | select(.ok == true and (.latencyMs|type == "number"))] | sort_by(.latencyMs) | .[] | [.id, (.latencyMs|tostring)] | @tsv' "$tmpdir/results.json" > "$tmpdir/candidates.tsv" 2>/dev/null || true
    while IFS="$(printf '\t')" read -r candidate_id candidate_ms; do
      [ -n "$candidate_id" ] || continue

      # If we reached the currently active healthy server after one or more
      # faster candidates failed real tunnel validation, keeping it is the
      # correct result; do not restart it pointlessly.
      if [ "$candidate_id" = "$current_id" ] && [ -n "$current_ms" ]; then
        if [ "$current_tunnel_ok" = "1" ]; then
          if [ "$tried" -gt 0 ]; then
            selected_id="$current_id"
            selected_ms="$current_ms"
            reason="faster endpoint(s) failed tunnel validation; kept current"
          fi
          break
        fi
        # Current can be first by ping while its actual HY2/TUIC tunnel is
        # dead. Re-applying identical credentials is pointless; try next.
        log "skip current candidate id=$candidate_id: egress validation failed"
        continue
      fi

      # Honour a user-configured hysteresis. Default is 0 ms, i.e. strict
      # lowest measured latency as requested; a positive value can be used to
      # reduce flapping on noisy links.
      if [ -n "$current_ms" ] && [ "$current_tunnel_ok" = "1" ]; then
        threshold="$(min_improvement_ms)"
        improvement=$((current_ms - candidate_ms))
        [ "$improvement" -lt "$threshold" ] && break
      fi

      tried=$((tried + 1))
      if apply_proxy "$candidate_id" "$profile_id"; then
        selected_id="$candidate_id"
        selected_ms="$candidate_ms"
        switched_ok=1
        break
      fi
      rc=$?
      log "candidate failed id=$candidate_id latency=${candidate_ms}ms rc=$rc; trying next"
      if [ "$rc" = "2" ]; then
        busy=1
        break
      fi
    done < "$tmpdir/candidates.tsv"

    if [ "$switched_ok" = "1" ]; then
      json_status ready "switched to lowest verified-latency server" true "$selected_id" "$selected_ms" "$selected_id" "$selected_ms" "$tmpdir/results.json"
      log "switch success id=$selected_id latency=${selected_ms}ms"
    elif [ "$busy" = "1" ]; then
      json_status error "apply busy; will retry next cycle" false "$best_id" "$best_ms" "$current_id" "$current_ms" "$tmpdir/results.json"
    elif [ -n "$selected_id" ] && [ "$selected_id" = "$current_id" ]; then
      json_status ready "$reason" false "$selected_id" "$selected_ms" "$current_id" "$current_ms" "$tmpdir/results.json"
    else
      json_status error "reachable servers failed real tunnel validation; kept current" false "$best_id" "$best_ms" "$current_id" "$current_ms" "$tmpdir/results.json"
      log "all switch candidates failed; current=$current_id"
    fi
  else
    json_status ready "$reason" false "$best_id" "$best_ms" "$current_id" "$current_ms" "$tmpdir/results.json"
  fi

  rm -rf "$tmpdir"
}

run_once() {
  RUN_LOCK=/tmp/antigoblin-autoselect-run.lock
  if ! mkdir "$RUN_LOCK" 2>/dev/null; then
    oldpid="$(cat "$RUN_LOCK/pid" 2>/dev/null || true)"
    case "$oldpid" in
      ''|*[!0-9]*) alive=0 ;;
      *) if kill -0 "$oldpid" 2>/dev/null; then alive=1; else alive=0; fi ;;
    esac
    if [ "$alive" = "0" ]; then
      rm -rf "$RUN_LOCK" 2>/dev/null || true
      mkdir "$RUN_LOCK" 2>/dev/null || return 2
    else
      return 2
    fi
  fi
  printf '%s\n' "$$" > "$RUN_LOCK/pid" 2>/dev/null || true
  run_once_inner "$@"
  rc=$?
  rm -rf "$RUN_LOCK" 2>/dev/null || true
  return "$rc"
}

case "${1:-}" in
  --loop)
    log "autoselect loop started"
    while :; do
      if is_enabled; then run_once 0 || true; fi
      sleep "$(interval_sec)"
    done
    ;;
  --once)
    run_once 0
    ;;
  --force)
    run_once 1
    ;;
  *)
    echo "Usage: $0 {--loop|--once|--force}" >&2
    exit 2
    ;;
esac
