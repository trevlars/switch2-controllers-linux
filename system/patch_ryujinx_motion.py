#!/usr/bin/env python3
"""Wire DSU/CemuHook motion into Ryujinx for the Switch 2 controllers.

Idempotent: for each player whose name identifies a Switch 2 controller, set its
motion block to the CemuHook backend pointing at the local DSU server, mapping it
to the matching DSU slot (NSO GameCube -> slot 0, Pro Controller 2 -> slot 1).
Other players are left untouched. A timestamped backup is written first.

Usage: python3 patch_ryujinx_motion.py [/path/to/Ryujinx/Config.json]
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".config/Ryujinx/Config.json"

# DSU slot per controller (matches the ngc bridge player order: player N -> slot N-1).
SLOT_BY_NAME = [
    ("pro controller 2", 1),
    ("gamecube", 0),
]


def cemuhook_motion(slot: int) -> dict:
    return {
        "slot": slot,
        "alt_slot": 0,
        "mirror_input": False,
        "dsu_server_host": "127.0.0.1",
        "dsu_server_port": 26760,
        "motion_backend": "CemuHook",
        "sensitivity": 100,
        "gyro_deadzone": 1,
        "enable_motion": True,
    }


def slot_for(name: str):
    low = name.lower()
    for needle, slot in SLOT_BY_NAME:
        if needle in low:
            return slot
    return None


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG
    if not path.is_file():
        print(f"Ryujinx config not found: {path}")
        return 1

    cfg = json.loads(path.read_text(encoding="utf-8"))
    inputs = cfg.get("input_config") or []
    changed = False
    for entry in inputs:
        slot = slot_for(entry.get("name", ""))
        if slot is None:
            continue
        desired = cemuhook_motion(slot)
        if entry.get("motion") != desired:
            entry["motion"] = desired
            entry["enable_motion"] = True
            changed = True
            print(f"  motion -> CemuHook slot {slot} for {entry.get('name')!r} "
                  f"({entry.get('player_index')})")

    if not changed:
        print("Ryujinx motion already configured; no changes.")
        return 0

    backup = path.with_suffix(f".json.bak.{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(path, backup)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"Wrote {path} (backup {backup.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
