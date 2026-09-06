#!/usr/bin/env bash
# Called when Switch 2 / NSO pads connect/disconnect (nso-gc NGC_AUTO_REORDER).
set -euo pipefail
export BAZZITE_REMOTE_XBOX_P1="${BAZZITE_REMOTE_XBOX_P1:-auto}"
# Prefer local ownership unless a live stream flag is present.
if [[ ! -f "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/bazzite-sunshine-stream-active" ]]; then
  export BAZZITE_REMOTE_XBOX_P1=never
  export BAZZITE_INCLUDE_VIRTUAL_XBOX=never
fi
# LEDs: Eden order for local (Pro=P1, GC=P2). Dolphin-first LEDs put Pro on 3
# and break Steam/Eden P1 on the account screen and in Switch games.
LED_MODE=eden
if [[ -f "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/bazzite-sunshine-stream-active" ]]; then
  LED_MODE=auto
fi
python3 "${HOME}/.local/bin/bazzite-controller-detect.py" \
  --dolphin --eden --leds "${LED_MODE}" --max-players 4 >/dev/null 2>&1 || true
