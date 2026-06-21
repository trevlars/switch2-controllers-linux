#!/usr/bin/env python3
"""Sweep vibration presets so we can map intensity. The player LEDs show the
preset number (1..4 LEDs, repeating) while each preset plays."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ngc.device import GameCubeController

logging.basicConfig(level=logging.WARNING)

DST = sys.argv[1] if len(sys.argv) > 1 else "AA:BB:CC:DD:EE:01"
ADAPTER = sys.argv[2] if len(sys.argv) > 2 else "00:00:00:00:00:00"
# 3rd arg: either a max preset number (e.g. "8") or a comma list (e.g. "2,3,5")
_spec = sys.argv[3] if len(sys.argv) > 3 else "8"
if "," in _spec:
    PRESETS = [int(x) for x in _spec.split(",")]
else:
    PRESETS = list(range(1, int(_spec) + 1))


def main() -> int:
    ctrl = GameCubeController(DST, ADAPTER)
    print("connecting (pairing mode if needed)...", flush=True)
    deadline = time.time() + 40
    while time.time() < deadline:
        if ctrl.connect(timeout=8):
            break
    else:
        print("could not connect")
        return 1
    ctrl.enable_commands()
    print(f"connected. Testing presets {PRESETS}; watch LEDs + feel intensity.\n", flush=True)
    for n in PRESETS:
        led = ((n - 1) % 4) + 1
        ctrl.set_player_leds(led)
        print(f">>> PRESET {n}  (LEDs showing {led})", flush=True)
        try:
            ctrl.play_vibration_preset(n)
        except Exception as exc:  # noqa: BLE001
            print(f"    preset {n} error: {exc}")
        time.sleep(3.0)
    ctrl.set_player_leds(1)
    print("\ndone", flush=True)
    ctrl.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
