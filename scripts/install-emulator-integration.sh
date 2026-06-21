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

# 1) controller-detect script
DETECT_DST="$HOME/.local/bin/bazzite-controller-detect.py"
if [ -f "$DETECT_DST" ]; then
  cp -p "$DETECT_DST" "$DETECT_DST.bak.$(ts)"
fi
install -m 0755 "$SYS/bazzite-controller-detect.py" "$DETECT_DST"
echo "installed $DETECT_DST"

# 2) Dolphin native GameCube profile
DOLPHIN_PROF="$HOME/.var/app/org.DolphinEmu.dolphin-emu/config/dolphin-emu/Profiles/GCPad"
if [ -d "$DOLPHIN_PROF" ]; then
  install -m 0644 "$SYS/dolphin/GC_nso_gamecube.ini" "$DOLPHIN_PROF/GC_nso_gamecube.ini"
  echo "installed $DOLPHIN_PROF/GC_nso_gamecube.ini"
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

echo "emulator-integration install complete"
