#!/usr/bin/env bash
# Keep Bluetooth on and reconnect trusted game controllers after wake/boot.
# Also re-asserts Steam Game Mode "Bluetooth Enabled" — Steam kills the
# adapter when config.vdf has Enabled=0 (common after Steam/profile reset).
set -euo pipefail

CONFIG="${HOME}/.config/bluetooth-reconnect-devices"
STEAM_CONFIG="${HOME}/.local/share/Steam/config/config.vdf"
RESUME="${1:-}"
NGC_STATE="${HOME}/.config/nso-gc/state.json"

ensure_steam_bluetooth_enabled() {
	local conf="$STEAM_CONFIG"
	[[ -f "$conf" ]] || return 0
	if grep -A2 '"Bluetooth"' "$conf" 2>/dev/null | grep -q '"Enabled"[[:space:]]*"0"'; then
		python3 - "$conf" <<'PY' 2>/dev/null || true
import re, sys
from pathlib import Path
p = Path(sys.argv[1])
text = p.read_text(errors="replace")
new, n = re.subn(
    r'("Bluetooth"\s*\{\s*"Enabled"\s*")0(")',
    r'\g<1>1\2',
    text,
    count=1,
)
if n:
    p.write_text(new)
    print("steam bluetooth Enabled -> 1", flush=True)
PY
	fi
}

ensure_adapter_powered() {
	rfkill unblock bluetooth 2>/dev/null || true
	bluetoothctl scan off 2>/dev/null || true
	busctl --timeout=5 set-property org.bluez /org/bluez/hci0 \
		org.bluez.Adapter1 Powered b true 2>/dev/null || true
	local _
	for _ in $(seq 1 25); do
		if bluetoothctl show 2>/dev/null | grep -q 'Powered: yes'; then
			return 0
		fi
		bluetoothctl power on 2>/dev/null || true
		busctl --timeout=5 set-property org.bluez /org/bluez/hci0 \
			org.bluez.Adapter1 Powered b true 2>/dev/null || true
		sleep 2
	done
	bluetoothctl power on 2>/dev/null || true
}

maybe_restart_nso_gc() {
	# try-restart kills live BLE sessions — only nudge the bridge when no pads
	# are connected via nso-gc.
	local connected=0
	if [[ -f "$NGC_STATE" ]]; then
		connected=$(python3 - "$NGC_STATE" <<'PY' 2>/dev/null || echo 0
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print(sum(1 for c in data.get("controllers", []) if c.get("connected")))
PY
)
	fi
	if [[ "$connected" -eq 0 ]]; then
		systemctl --user try-restart nso-gc.service 2>/dev/null || true
	fi
}

ensure_steam_bluetooth_enabled
ensure_adapter_powered

if [[ "$RESUME" == "--resume" ]]; then
	sleep 6
	ensure_steam_bluetooth_enabled
	ensure_adapter_powered
elif [[ "$RESUME" == "--watchdog" ]]; then
	: # power already asserted; reconnect below
else
	sleep 2
	sleep 12
	ensure_steam_bluetooth_enabled
	ensure_adapter_powered
fi

is_gaming_pad() {
	local mac="$1"
	bluetoothctl info "$mac" 2>/dev/null | grep -q 'Icon: input-gaming'
}

already_connected() {
	local mac="$1"
	bluetoothctl info "$mac" 2>/dev/null | grep -q 'Connected: yes'
}

device_available() {
	local mac="$1"
	local info
	info=$(bluetoothctl info "$mac" 2>/dev/null) || return 1
	echo "$info" | grep -q 'Connected: yes' && return 0
	echo "$info" | grep -q 'RSSI:' || return 1
	return 0
}

try_connect() {
	local mac="$1"
	local tries="${2:-6}"

	already_connected "$mac" && return 0
	device_available "$mac" || return 1
	bluetoothctl trust "$mac" 2>/dev/null || true

	local i
	for i in $(seq 1 "$tries"); do
		if bluetoothctl connect "$mac" 2>/dev/null | grep -qiE 'successful|already'; then
			sleep 1
			already_connected "$mac" && return 0
		fi
		sleep 3
	done
	return 1
}

reconnect_list() {
	local mac
	if [[ -f "$CONFIG" ]]; then
		while read -r mac; do
			[[ -z "$mac" || "$mac" =~ ^# ]] && continue
			try_connect "$mac" 8 || true
		done <"$CONFIG"
	fi

	while read -r mac _; do
		[[ -z "$mac" ]] && continue
		if is_gaming_pad "$mac"; then
			grep -qxi "$mac" "$CONFIG" 2>/dev/null && continue
			device_available "$mac" || continue
			try_connect "$mac" 3 || true
		fi
	done < <(bluetoothctl devices 2>/dev/null | awk '{print $2}')
}

reconnect_list
bluetoothctl scan off 2>/dev/null || true
maybe_restart_nso_gc
