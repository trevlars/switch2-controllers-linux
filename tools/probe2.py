#!/usr/bin/env python3
"""Live test over the raw ATT client: connect, handshake, stream decoded input
(buttons, sticks, analog triggers)."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ngc.device import GameCubeController

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DST = sys.argv[1] if len(sys.argv) > 1 else "AA:BB:CC:DD:EE:01"
ADAPTER = sys.argv[2] if len(sys.argv) > 2 else "00:00:00:00:00:00"


def main() -> int:
    ctrl = GameCubeController(DST, ADAPTER)
    print(f"connecting (raw L2CAP) to {DST} ...", flush=True)
    deadline = time.time() + 40
    while time.time() < deadline:
        if ctrl.connect(timeout=8):
            break
    else:
        print("could not connect (keep controller in pairing mode)")
        return 1
    print(f"connected, MTU={ctrl.att.mtu}; initializing...", flush=True)
    ctrl.initialize(player=1)
    print(f"ready: {ctrl.info.name} serial={ctrl.info.serial_number}\n", flush=True)

    last = [""]
    count = [0]

    def on_input(c: GameCubeController, r):
        count[0] += 1
        (lx, ly), (rx, ry), lt, rt = c.calibrated_input(r)
        line = (
            f"L({lx:+.2f},{ly:+.2f}) C({rx:+.2f},{ry:+.2f}) "
            f"LT={lt:3d} RT={rt:3d} batt={r.battery_mv}mV [{' '.join(r.pressed())}]"
        )
        if line != last[0]:
            last[0] = line
            print("\r" + line.ljust(110), end="", flush=True)

    ctrl.input_callback = on_input
    print("Streaming. Move sticks / squeeze triggers / press buttons. Ctrl-C to exit.\n", flush=True)
    try:
        t0 = time.time()
        while ctrl.is_connected:
            time.sleep(2)
            dt = time.time() - t0
            if dt > 0 and count[0]:
                print(f"\n  [{count[0]} reports, ~{count[0]/dt:.0f} Hz]", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        ctrl.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
