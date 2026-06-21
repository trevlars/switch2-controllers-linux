#!/usr/bin/env python3
"""Read events from a virtual gamepad device and summarise which buttons/axes
fired. Used to verify the bridge's uinput mapping."""

import sys
import time

import evdev
from evdev import ecodes as e

path = sys.argv[1]
duration = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

d = evdev.InputDevice(path)
keys = set()
abs_seen = {}
t0 = time.time()
try:
    for ev in d.read_loop():
        if time.time() - t0 > duration:
            break
        if ev.type == e.EV_KEY and ev.value == 1:
            keys.add(ev.code)
        elif ev.type == e.EV_ABS:
            lohi = abs_seen.setdefault(ev.code, [ev.value, ev.value])
            lohi[0] = min(lohi[0], ev.value)
            lohi[1] = max(lohi[1], ev.value)
except KeyboardInterrupt:
    pass

key_lookup = evdev.ecodes.bytype[e.EV_KEY]
abs_lookup = evdev.ecodes.bytype[e.EV_ABS]
print("\nKEYS pressed:")
for c in sorted(keys):
    print(f"   {key_lookup.get(c)} ({c})")
print("ABS axes moved (min..max):")
for c, (lo, hi) in sorted(abs_seen.items()):
    if lo != hi:
        print(f"   {abs_lookup.get(c)} ({c}): {lo}..{hi}")
