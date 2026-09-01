#!/usr/bin/env bash
# Restart nso-gc when the hub is running but saved pads never connect (stuck BLE scanner).
# Never restart while pads are connected — that tears down live sessions.
set -euo pipefail

SERVICE=nso-gc.service
STATE="${HOME}/.config/nso-gc/state.json"
GRACE_AFTER_START_S=120
STUCK_NO_CONNECT_S=600
STALE_STATE_S=90

is_active() {
  [[ "$(systemctl --user is-active "$SERVICE" 2>/dev/null || true)" == "active" ]]
}

service_uptime_s() {
  systemctl --user show "$SERVICE" -p ActiveEnterTimestamp --value 2>/dev/null \
    | python3 -c 'import sys,datetime; s=sys.stdin.read().strip();
try:
  if not s: print(0); raise SystemExit
  dt=datetime.datetime.fromisoformat(s)
  print(max(0,int((datetime.datetime.now(dt.tzinfo or datetime.timezone.utc)-dt).total_seconds())))
except Exception:
  print(0)'
}

restart_bridge() {
  logger -t nso-gc-watchdog "$1"
  systemctl --user reset-failed "$SERVICE" 2>/dev/null || true
  systemctl --user restart "$SERVICE"
}

is_active || exit 0

uptime_s="$(service_uptime_s)"
[[ "$uptime_s" -ge "$GRACE_AFTER_START_S" ]] || exit 0

python3 - "$STATE" "$STUCK_NO_CONNECT_S" "$STALE_STATE_S" "$uptime_s" << 'PY'
import json, sys, time
from pathlib import Path

state_path, stuck_s, stale_s, uptime_s = sys.argv[1:5]
stuck_s, stale_s, uptime_s = int(stuck_s), int(stale_s), int(uptime_s)
path = Path(state_path)
if not path.is_file():
    sys.exit(0)
try:
    data = json.loads(path.read_text())
except Exception:
    print("restart: unreadable state.json")
    sys.exit(2)

controllers = data.get("controllers") or []
configured = len(controllers)
connected = sum(1 for c in controllers if c.get("connected"))
if connected > 0:
    sys.exit(0)

updated = float(data.get("updated_at") or 0)
age = time.time() - updated if updated else 9999
hub_error = (data.get("hub_error") or "").strip()
hub_alive = bool(data.get("hub_alive"))

if hub_error:
    print(f"restart: hub_error={hub_error[:120]}")
    sys.exit(2)

if configured and connected == 0 and hub_alive and age <= stale_s and uptime_s >= stuck_s:
    print(f"restart: {configured} saved pad(s), 0 connected for {uptime_s}s")
    sys.exit(2)

if configured and age > stale_s and uptime_s >= 300:
    print(f"restart: stale state.json ({age:.0f}s old) while service active")
    sys.exit(2)

if configured and connected == 0 and uptime_s >= stuck_s:
    import subprocess
    j = subprocess.run(
        ["journalctl", "--user", "-u", "nso-gc.service", "--since", "15 min ago", "-o", "cat"],
        capture_output=True, text=True,
    )
    log = j.stdout or ""
    if "saw " in log and "connected " not in log and "virtual gamepad ready" not in log:
        print("restart: adverts seen in journal but no successful connect")
        sys.exit(2)

sys.exit(0)
PY
rc=$?
if [[ "$rc" -eq 2 ]]; then
  restart_bridge "watchdog triggered"
fi
