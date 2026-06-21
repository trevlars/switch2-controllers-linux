#!/usr/bin/env python3
"""Upload force-feedback effects to the virtual gamepad and play them, so we can
confirm the rumble back-channel reaches the physical controller."""

import sys
import time

import evdev
from evdev import ecodes as e, ff


def find_device() -> evdev.InputDevice:
    for path in evdev.list_devices():
        d = evdev.InputDevice(path)
        if "GameCube" in (d.name or ""):
            return d
    raise SystemExit("virtual gamepad not found (is the service running?)")


def play(dev: evdev.InputDevice, strong: int, weak: int, ms: int, label: str) -> None:
    rumble = ff.Rumble(strong_magnitude=strong, weak_magnitude=weak)
    effect = ff.Effect(
        e.FF_RUMBLE, -1, 0,
        ff.Trigger(0, 0),
        ff.Replay(ms, 0),
        ff.EffectType(ff_rumble_effect=rumble),
    )
    eid = dev.upload_effect(effect)
    print(f"  playing {label}: strong={strong} weak={weak} for {ms}ms", flush=True)
    dev.write(e.EV_FF, eid, 1)
    time.sleep(ms / 1000 + 0.3)
    dev.erase_effect(eid)
    time.sleep(0.4)


def main() -> int:
    dev = find_device()
    print(f"device: {dev.path} ({dev.name})")
    print(f"FF capable: {e.EV_FF in dev.capabilities()}")
    play(dev, 0xFFFF, 0x0000, 800, "STRONG motor")
    play(dev, 0x0000, 0xFFFF, 800, "WEAK motor")
    play(dev, 0xFFFF, 0xFFFF, 1000, "BOTH motors")
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
