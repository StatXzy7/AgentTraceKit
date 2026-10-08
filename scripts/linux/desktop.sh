#!/usr/bin/env bash
set -euo pipefail
export DISPLAY=:99
export XAUTHORITY=/home/atk/.Xauthority
umask 077
touch "$XAUTHORITY"
chmod 600 "$XAUTHORITY"
cookie=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
xauth -f "$XAUTHORITY" add "$DISPLAY" . "$cookie"
children=()
cleanup() {
  trap - EXIT TERM INT
  for pid in "${children[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait || true
}
trap cleanup EXIT TERM INT
Xvfb "$DISPLAY" -screen 0 1280x720x24 -nolisten tcp -auth "$XAUTHORITY" &
children+=("$!")
for attempt in {1..50}; do
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then break; fi
  sleep 0.2
done
xdpyinfo -display "$DISPLAY" >/dev/null
openbox &
children+=("$!")
x11vnc -display "$DISPLAY" -auth "$XAUTHORITY" -localhost -rfbport 5900 -forever -shared -nopw -noxdamage &
children+=("$!")
websockify --web /usr/share/novnc 127.0.0.1:6080 127.0.0.1:5900 &
children+=("$!")
wait -n
exit 1
