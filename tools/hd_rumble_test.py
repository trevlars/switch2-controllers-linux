#!/usr/bin/env python3
"""Sweep several HD-rumble waveform profiles on a Pro Controller 2 / Joy-Con 2 so
we can pick the one that feels most like a natural gamepad rumble.

Each profile is held for ~2.5s (re-sent at 60 Hz for a smooth envelope) with the
player LEDs showing the profile number, then a short gap. Tell me which numbers
feel best.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ngc.bridge import prepare_bluez
from ngc.device import SwitchController
from ngc.protocol import VibrationData

DST = sys.argv[1] if len(sys.argv) > 1 else "AA:BB:CC:DD:EE:02"
ADAPTER = sys.argv[2] if len(sys.argv) > 2 else "00:00:00:00:00:00"

# (label, VibrationData). Explore frequency, single vs dual band, and tone bit.
PROFILES = [
    ("1 low-freq heavy", VibrationData(lf_freq=0x041, lf_amp=0x3FF)),
    ("2 default low band", VibrationData(lf_freq=0x0E1, lf_amp=0x3FF)),
    ("3 dual band balanced", VibrationData(lf_freq=0x0E1, lf_amp=0x320, hf_freq=0x1E1, hf_amp=0x1C0)),
    ("4 dual band + tone", VibrationData(lf_freq=0x0E1, lf_en_tone=True, lf_amp=0x320,
                                          hf_freq=0x1E1, hf_en_tone=True, hf_amp=0x1C0)),
    ("5 high-freq light", VibrationData(hf_freq=0x1E1, hf_amp=0x260)),
]


def hold(ctrl: SwitchController, vib: VibrationData, seconds: float, hz: float = 60.0) -> None:
    end = time.time() + seconds
    period = 1.0 / hz
    while time.time() < end:
        ctrl._write_motor(vib)
        time.sleep(period)
    # stop
    ctrl._write_motor(VibrationData())


def main() -> int:
    prepare_bluez(DST)
    ctrl = SwitchController(DST, ADAPTER)
    print(f"connecting to {DST} ...", flush=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        if ctrl.connect(timeout=8):
            break
    else:
        print("could not connect (wake the controller)")
        return 1

    ctrl._resolve_handles()
    ctrl.enable_commands()
    ctrl.info = ctrl.read_controller_info()
    ctrl._resolve_vibration_handle()
    print(f"connected: {ctrl.name} vibration={ctrl.h_vibration:#06x}\n", flush=True)

    for idx, (label, vib) in enumerate(PROFILES, start=1):
        ctrl.set_player_leds(((idx - 1) % 4) + 1)
        print(f">>> PROFILE {label}", flush=True)
        hold(ctrl, vib, 2.5)
        time.sleep(1.2)

    print("\ndone", flush=True)
    ctrl.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
