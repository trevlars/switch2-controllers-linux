#!/usr/bin/env python3
"""Test the corrected GameCube motor-vibration packet, and confirm the
controller does NOT power off afterwards."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ngc import protocol as P
from ngc.device import GameCubeController

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DST = sys.argv[1] if len(sys.argv) > 1 else "AA:BB:CC:DD:EE:01"
ADAPTER = sys.argv[2] if len(sys.argv) > 2 else "00:00:00:00:00:00"

alive = {"v": True}


def main() -> int:
    ctrl = GameCubeController(DST, ADAPTER)
    ctrl.disconnect_callback = lambda: alive.__setitem__("v", False)

    print("connecting (pairing mode if needed)...", flush=True)
    deadline = time.time() + 40
    while time.time() < deadline:
        if ctrl.connect(timeout=8):
            break
    else:
        print("could not connect")
        return 1
    ctrl.initialize(player=1)
    print("connected + initialized (you should have felt the connect preset)\n", flush=True)
    time.sleep(1)

    print("[A] sending corrected MOTOR vibration (lf full) for 1.5s...", flush=True)
    t = time.time()
    while time.time() - t < 1.5 and alive["v"]:
        ctrl.set_vibration(P.VibrationData(lf_amp=0x3FF))
        time.sleep(0.1)
    ctrl.set_vibration(P.VibrationData())
    print(f"   after motor vibration: connected={alive['v']}", flush=True)

    if alive["v"]:
        time.sleep(1)
        print("[B] sending hf full for 1.5s...", flush=True)
        t = time.time()
        while time.time() - t < 1.5 and alive["v"]:
            ctrl.set_vibration(P.VibrationData(hf_amp=0x3FF))
            time.sleep(0.1)
        ctrl.set_vibration(P.VibrationData())
        print(f"   after hf vibration: connected={alive['v']}", flush=True)

    print("\n[C] holding 12s to confirm it stays on...", flush=True)
    t = time.time()
    while time.time() - t < 12 and alive["v"]:
        time.sleep(2)
        print(f"   alive={alive['v']}", flush=True)

    print(f"\nFINAL connected={alive['v']}", flush=True)
    ctrl.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
