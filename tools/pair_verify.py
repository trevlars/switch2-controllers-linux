#!/usr/bin/env python3
"""Connect to a specific Switch 2 controller MAC, bond it, verify input parsing,
and test rumble. Used to bring up a new controller (e.g. Pro Controller 2)
without touching the saved bridge config."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ngc.bridge import prepare_bluez
from ngc.device import SwitchController

DST = sys.argv[1]
ADAPTER = sys.argv[2] if len(sys.argv) > 2 else "00:00:00:00:00:00"
DO_BOND = "--bond" in sys.argv


def main() -> int:
    prepare_bluez(DST)
    ctrl = SwitchController(DST, ADAPTER)
    print(f"connecting to {DST} ...", flush=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        if ctrl.connect(timeout=8):
            break
    else:
        print("could not connect (keep controller in pairing mode)")
        return 1

    print(f"connected MTU={ctrl.att.mtu}", flush=True)
    ctrl._resolve_handles()
    ctrl.enable_commands()
    ctrl.info = ctrl.read_controller_info()
    ctrl._resolve_vibration_handle()
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

    ctrl._read_calibration()
    ctrl.set_player_leds(1)
    ctrl.enable_features(0x03 | 0x04)
    if ctrl.has_hd_rumble:
        ctrl._start_hd_worker()

    if DO_BOND:
        ctrl.bond()
        print("BONDED (will reconnect without pairing mode)", flush=True)

    # Rumble test across realistic force-feedback levels (uses tuned mapping).
    for label, (s, w) in [
        ("strong (1.0)", (1.0, 0.0)),
        ("medium (0.5)", (0.5, 0.0)),
        ("light  (0.25)", (0.25, 0.0)),
        ("weak motor (0.6)", (0.0, 0.6)),
    ]:
        print(f"rumble: {label} ...", flush=True)
        ctrl.set_rumble(s, w)
        time.sleep(1.5)
        ctrl.set_rumble(0.0, 0.0)
        time.sleep(0.6)
    print("rumble done", flush=True)

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
    ctrl.att.subscribe(ctrl.h_input_cccd, True)
    print("streaming 10s; move sticks, press ZL/ZR + buttons ...", flush=True)
    time.sleep(10)
    print(f"received {count[0]} reports", flush=True)
    ctrl.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
