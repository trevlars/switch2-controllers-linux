#!/usr/bin/env python3
"""Connect to whatever Switch 2 controller is configured/awake, run the full
initialize() (dynamic discovery + kind detection), print resolved handles and
identity, and stream a few input reports to confirm parsing. Works for the
GameCube pad and the Pro Controller 2."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ngc.device import SwitchController

DST = sys.argv[1] if len(sys.argv) > 1 else "AA:BB:CC:DD:EE:01"
ADAPTER = sys.argv[2] if len(sys.argv) > 2 else "00:00:00:00:00:00"


def main() -> int:
    ctrl = SwitchController(DST, ADAPTER)
    print(f"connecting to {DST} ...", flush=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        if ctrl.connect(timeout=8):
            break
    else:
        print("could not connect (wake / pairing-mode the controller)")
        return 1

    print(f"connected MTU={ctrl.att.mtu}", flush=True)
    ctrl.initialize(player=1)
    print(
        f"identity: {ctrl.name} pid={ctrl.product_id:#06x} "
        f"hd_rumble={ctrl.has_hd_rumble} analog_triggers={ctrl.has_analog_triggers}",
        flush=True,
    )
    print(
        f"handles input={ctrl.h_input:#06x}/{ctrl.h_input_cccd:#06x} "
        f"cmd_w={ctrl.h_cmd_write:#06x} cmd_r={ctrl.h_cmd_resp:#06x}/{ctrl.h_cmd_resp_cccd:#06x} "
        f"vibration={ctrl.h_vibration:#06x}",
        flush=True,
    )

    count = [0]

    def on_input(c, report):
        count[0] += 1
        if count[0] % 30 == 0:
            (lx, ly), (rx, ry), lt, rt = c.calibrated_input(report)
            btns = ",".join(report.pressed()) or "-"
            print(
                f"L=({lx:+.2f},{ly:+.2f}) R=({rx:+.2f},{ry:+.2f}) "
                f"LT={lt:3d} RT={rt:3d} batt={report.battery_mv}mV btns={btns}",
                flush=True,
            )

    ctrl.input_callback = on_input
    print("streaming 8s; move sticks / press triggers ...", flush=True)
    time.sleep(8)
    print(f"received {count[0]} reports", flush=True)
    ctrl.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
