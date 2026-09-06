#!/usr/bin/env bash
# Install the emulator-integration pieces on Bazzite (run on the box, from the
# rsynced ~/nso-gc-bazzite). Idempotent; backs up files it replaces.
#   - controller-detect script (new pad kinds + P1-P4 order)
#   - Dolphin native GameCube profile for the NSO GameCube pad
#   - Ryujinx Switch 2 Pro profile + CemuHook (DSU) motion wiring
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SYS="$PROJECT_DIR/system"
ts() { date +%Y%m%d%H%M%S; }

# 1) controller-detect script (includes --eden; never clobber a newer live copy
# that already has Eden multi-player sync unless the repo copy is newer/larger)
DETECT_DST="$HOME/.local/bin/bazzite-controller-detect.py"
DETECT_SRC="$SYS/bazzite-controller-detect.py"
if [ -f "$DETECT_DST" ] && grep -q -- '--eden' "$DETECT_DST" 2>/dev/null; then
  if ! grep -q -- '--eden' "$DETECT_SRC" 2>/dev/null; then
    echo "keep $DETECT_DST (live has --eden; repo copy does not)"
  elif [ "$(wc -c < "$DETECT_DST")" -gt "$(wc -c < "$DETECT_SRC")" ]; then
    echo "keep $DETECT_DST (live larger than repo; likely the tuned Eden tool)"
  else
    cp -p "$DETECT_DST" "$DETECT_DST.bak.$(ts)"
    install -m 0755 "$DETECT_SRC" "$DETECT_DST"
    echo "installed $DETECT_DST"
  fi
else
  if [ -f "$DETECT_DST" ]; then
    cp -p "$DETECT_DST" "$DETECT_DST.bak.$(ts)"
  fi
  install -m 0755 "$DETECT_SRC" "$DETECT_DST"
  echo "installed $DETECT_DST"
fi

# 2) Dolphin native GameCube profile
DOLPHIN_PROF="$HOME/.var/app/org.DolphinEmu.dolphin-emu/config/dolphin-emu/Profiles/GCPad"
if [ -d "$DOLPHIN_PROF" ]; then
  install -m 0644 "$SYS/dolphin/GC_nso_gamecube.ini" "$DOLPHIN_PROF/GC_nso_gamecube.ini"
  echo "installed $DOLPHIN_PROF/GC_nso_gamecube.ini"
  if [ -f "$SYS/dolphin/GC_switch2_pro_bt.ini" ]; then
    install -m 0644 "$SYS/dolphin/GC_switch2_pro_bt.ini" \
      "$DOLPHIN_PROF/GC_switch2_pro_bt.ini"
    echo "installed $DOLPHIN_PROF/GC_switch2_pro_bt.ini"
  fi
else
  echo "skip Dolphin profile (dir not found)"
fi

# 3) Ryujinx Switch 2 Pro profile
RYU_PROF="$HOME/.config/Ryujinx/profiles/controller"
if [ -d "$RYU_PROF" ]; then
  install -m 0644 "$SYS/ryujinx/Switch2_Pro.json" "$RYU_PROF/Switch2_Pro.json"
  echo "installed $RYU_PROF/Switch2_Pro.json"
else
  echo "skip Ryujinx profile (dir not found)"
fi

# 4) Ryujinx CemuHook (DSU) motion wiring on the live config
RYU_CFG="$HOME/.config/Ryujinx/Config.json"
if [ -f "$RYU_CFG" ]; then
  python3 "$SYS/patch_ryujinx_motion.py" "$RYU_CFG" || true
fi

# 5) Dusklight / Twilight Princess — fix NSO GameCube .controller bindings
if [ -x "$PROJECT_DIR/scripts/install-dusklight-gc.sh" ]; then
  bash "$PROJECT_DIR/scripts/install-dusklight-gc.sh" || true
fi

# 6) Eden — thin wrapper that calls controller-detect --eden (never the old
# full qt-config rewriter that wiped Controls / forced manual rebind)
EDEN_RESET="$HOME/.local/bin/bazzite-eden-reset-controllers.py"
EDEN_SRC="$SYS/bazzite-eden-reset-controllers.py"
if [ -f "$EDEN_RESET" ] && grep -q 'controller-detect.py.*--eden\|DETECTOR.*--eden' "$EDEN_RESET" 2>/dev/null; then
  echo "keep $EDEN_RESET (already thin --eden wrapper)"
else
  if [ -f "$EDEN_RESET" ]; then
    cp -p "$EDEN_RESET" "$EDEN_RESET.bak.$(ts)"
  fi
  install -m 0755 "$EDEN_SRC" "$EDEN_RESET"
  echo "installed $EDEN_RESET"
fi
if [ -f "$HOME/.config/eden/qt-config.ini" ]; then
  BAZZITE_STEAM_INPUT_FALLBACK=never python3 "$EDEN_RESET" || true
fi

# 7) SDL gamecontrollerdb — ngc GameCube line only in Applications DB (never truncate Steam)
PY="${PROJECT_DIR}/.venv312/bin/python3"
if [ ! -x "$PY" ]; then
  PY="${PROJECT_DIR}/.venv/bin/python3"
fi
if [ -x "$PY" ]; then
  "$PY" "$PROJECT_DIR/tools/sdl_guid.py" --restore-dbs || true
  if systemctl --user is-active --quiet nso-gc.service 2>/dev/null; then
    "$PY" "$PROJECT_DIR/tools/sdl_guid.py" --write --wait 8 || true
  else
    echo "skip ngc gamecontrollerdb write (start nso-gc.service, wake pads, then: tools/sdl_guid.py --write)"
  fi
else
  echo "skip gamecontrollerdb (no project venv)"
fi

# 8) Player LED sync (sysfs DualSense/EXLENE/N64 + bridge led-players.json)
LED_DST="$HOME/.local/bin/bazzite-set-player-leds.py"
install -m 0755 "$SYS/bazzite-set-player-leds.py" "$LED_DST"
echo "installed $LED_DST"
install -m 0755 "$SYS/bazzite-reorder-on-nso-connect.sh" \
  "$HOME/.local/bin/bazzite-reorder-on-nso-connect.sh"
echo "installed $HOME/.local/bin/bazzite-reorder-on-nso-connect.sh"
install -m 0755 "$SYS/bluetooth-always-on.sh" "$HOME/.local/bin/bluetooth-always-on.sh"
echo "installed $HOME/.local/bin/bluetooth-always-on.sh"
install -m 0755 "$SYS/bazzite-nso-gc-watchdog.sh" \
  "$HOME/.local/bin/bazzite-nso-gc-watchdog.sh"
echo "installed $HOME/.local/bin/bazzite-nso-gc-watchdog.sh"
if [ -f "$PROJECT_DIR/tools/bazzite-controller-status" ]; then
  install -m 0755 "$PROJECT_DIR/tools/bazzite-controller-status" \
    "$HOME/.local/bin/bazzite-controller-status"
  echo "installed $HOME/.local/bin/bazzite-controller-status"
fi
LED_PERMS_SRC="$SYS/bazzite-fix-controller-led-perms"
LED_PERMS_DST="/usr/local/bin/bazzite-fix-controller-led-perms"
if [ -f "$LED_PERMS_SRC" ]; then
  if sudo -n true 2>/dev/null; then
    sudo install -m 0755 "$LED_PERMS_SRC" "$LED_PERMS_DST"
    echo "installed $LED_PERMS_DST"
  else
    echo "skip $LED_PERMS_DST (need passwordless sudo once):"
    echo "  sudo install -m 0755 $LED_PERMS_SRC $LED_PERMS_DST"
  fi
fi

# 9) udev: IMU uaccess + writable player LEDs
for rule in 70-ngc-imu-uaccess.rules 71-controller-player-leds-uaccess.rules; do
  src="$SYS/udev/$rule"
  dst="/etc/udev/rules.d/$rule"
  [ -f "$src" ] || continue
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    echo "udev $rule already current"
    continue
  fi
  if sudo -n true 2>/dev/null; then
    sudo install -m 0644 "$src" "$dst"
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=leds --action=add 2>/dev/null || true
    echo "installed $dst"
  else
    echo "skip $dst (need sudo once):"
    echo "  sudo cp $src $dst && sudo udevadm control --reload-rules"
  fi
done

echo "emulator-integration install complete"
