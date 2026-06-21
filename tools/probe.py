#!/usr/bin/env python3
"""Live diagnostic: scan for an NSO GameCube controller, connect, run the
handshake, and stream decoded input to the terminal.

Run on the Bazzite box:
    cd ~/nso-gc-bazzite && .venv/bin/python tools/probe.py
Put the controller in pairing mode first (hold the small sync button until the
player LEDs scan back and forth).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ngc import protocol as P
from ngc.controller import Controller
from ngc.scanner import find_first

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("probe")


async def main() -> int:
    addr = sys.argv[1] if len(sys.argv) > 1 else None
    if addr:
        print(f"Connecting directly to {addr} ...")
        ctrl = Controller(addr)
    else:
        print("Scanning for an NSO GameCube controller (put it in pairing mode)...")
        found = await find_first(timeout=25.0, only_pids={P.NSO_GAMECUBE_PID})
        if not found:
            print("No controller found. Hold the sync button and retry.")
            return 1
        print(f"Found {found.name} at {found.device.address}; connecting...")
        ctrl = Controller(found.device)
    await ctrl.connect(timeout=40.0)

    print(f"Connected: {ctrl.info.name}  serial={ctrl.info.serial_number}")
    print(f"  VID:PID = {ctrl.info.vendor_id:04X}:{ctrl.info.product_id:04X}")
    print(f"  left stick calib  = {ctrl.left_calib}")
    print(f"  right stick calib = {ctrl.right_calib}")
    print(f"  gc trigger neutral = {ctrl.trigger_neutral}")

    await ctrl.set_player_leds(1)
    await ctrl.play_vibration_preset(0x03)
    await ctrl.enable_features(P.FEATURE_MOTION)

    last_line = [""]

    def on_input(c: Controller, r: P.InputReport) -> None:
        lx, ly = c.left_calib.apply(r.left_stick_raw) if c.left_calib else (0, 0)
        rx, ry = c.right_calib.apply(r.right_stick_raw) if c.right_calib else (0, 0)
        lt = P.normalize_trigger(r.left_trigger_raw, c.trigger_neutral[0])
        rt = P.normalize_trigger(r.right_trigger_raw, c.trigger_neutral[1])
        line = (
            f"L({lx:+.2f},{ly:+.2f}) R({rx:+.2f},{ry:+.2f}) "
            f"LT={lt:3d} RT={rt:3d} batt={r.battery_mv}mV "
            f"[{' '.join(r.pressed())}]"
        )
        if line != last_line[0]:
            last_line[0] = line
            print("\r" + line.ljust(110), end="", flush=True)

    ctrl.input_callback = on_input
    await ctrl.start_input()
    print("Streaming input. Press buttons / move sticks / squeeze triggers. Ctrl-C to exit.\n")

    try:
        while ctrl.is_connected:
            await asyncio.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        await ctrl.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
