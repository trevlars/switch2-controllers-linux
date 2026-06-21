#!/usr/bin/env python3
"""One-shot live diagnostic: connect, confirm input is alive, then try several
vibration variants (so we can find what makes the GameCube controller rumble),
print gyro, and report connection stability. Run with the service stopped."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ngc import protocol as P
from ngc.device import GameCubeController, HANDLE_VIBRATION

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DST = sys.argv[1] if len(sys.argv) > 1 else "AA:BB:CC:DD:EE:01"
ADAPTER = sys.argv[2] if len(sys.argv) > 2 else "00:00:00:00:00:00"

state = {"count": 0, "last_report": None, "connected": True}


def main() -> int:
    ctrl = GameCubeController(DST, ADAPTER)

    def on_disc():
        state["connected"] = False
        print("\n!!! controller DISCONNECTED", flush=True)

    ctrl.disconnect_callback = on_disc

    print("connecting (put controller in pairing mode)...", flush=True)
    deadline = time.time() + 40
    while time.time() < deadline:
        if ctrl.connect(timeout=8):
            break
    else:
        print("could not connect")
        return 1
    print("connected; initializing", flush=True)
    ctrl.initialize(player=1)

    def on_input(c, r):
        state["count"] += 1
        state["last_report"] = r

    ctrl.input_callback = on_input

    # 1) Confirm input is alive
    print("\n[1] reading input for 3s (move a stick)...", flush=True)
    time.sleep(3)
    r = state["last_report"]
    if r:
        print(f"    reports={state['count']} buttons={r.pressed()} batt={r.battery_mv}mV", flush=True)
        print(f"    gyro={r.gyro} accel={r.accel}", flush=True)
    if not state["connected"]:
        print("    controller dropped during idle read"); return 2

    # 2) Built-in vibration presets
    print("\n[2] vibration PRESETS 1..6 (1.5s each)...", flush=True)
    for pid in range(1, 7):
        if not state["connected"]:
            break
        print(f"    preset {pid}", flush=True)
        try:
            ctrl.play_vibration_preset(pid)
        except Exception as exc:  # noqa: BLE001
            print(f"      preset error: {exc}")
        time.sleep(1.5)

    # 3) Motor vibration variants on the GC vibration characteristic
    print("\n[3] motor vibration variants (2s each)...", flush=True)
    variants = [
        ("lf full, no tone", P.VibrationData(lf_amp=0x3FF)),
        ("hf full, no tone", P.VibrationData(hf_amp=0x3FF)),
        ("both full, no tone", P.VibrationData(lf_amp=0x3FF, hf_amp=0x3FF)),
        ("both full, tone on", P.VibrationData(lf_amp=0x3FF, hf_amp=0x3FF, lf_en_tone=True, hf_en_tone=True)),
    ]
    for label, vib in variants:
        if not state["connected"]:
            break
        print(f"    {label}", flush=True)
        # send repeatedly for ~1.5s in case it is consumed per-packet
        t = time.time()
        while time.time() - t < 1.5 and state["connected"]:
            try:
                ctrl.set_vibration(vib)
            except Exception as exc:  # noqa: BLE001
                print(f"      vib error: {exc}")
                break
            time.sleep(0.1)
        ctrl.set_vibration(P.VibrationData())
        time.sleep(0.7)

    # 4) Stability watch
    print("\n[4] holding connection 20s to observe stability...", flush=True)
    t = time.time()
    while time.time() - t < 20 and state["connected"]:
        time.sleep(2)
        print(f"    alive, reports={state['count']}", flush=True)

    print(f"\nfinal: connected={state['connected']} total_reports={state['count']}", flush=True)
    ctrl.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
