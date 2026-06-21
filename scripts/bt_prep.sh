#!/usr/bin/env bash
# Prepare BlueZ so a raw L2CAP connection can be initiated:
#  - stop the Decky "BT Wake Control" background scan (if present)
#  - stop bluetoothctl discovery
#  - forget the controller so BlueZ won't auto-connect/grab it
# Safe to run repeatedly. Does not require sudo.
set -u

MAC="${1:-AA:BB:CC:DD:EE:01}"

pkill -f decky-bluetooth-wake-control 2>/dev/null && echo "stopped decky bt-wake scan" || true
bluetoothctl scan off >/dev/null 2>&1 || true
bluetoothctl remove "$MAC" >/dev/null 2>&1 || true
sleep 1
echo "Discovering: $(bluetoothctl show | grep -oE 'Discovering: (yes|no)')"
