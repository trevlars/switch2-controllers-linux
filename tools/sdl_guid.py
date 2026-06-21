#!/usr/bin/env python3
"""Print SDL GUIDs + GameController mappings for connected pads, and optionally
append the SDL-generated mappings to gamecontrollerdb.txt files.

Run this ON the Bazzite box with the controllers awake/connected:
    python3 tools/sdl_guid.py            # list pads + GUID + mapping
    python3 tools/sdl_guid.py --write     # append ngc pads to gamecontrollerdb

The SDL-generated mapping is authoritative (it reflects exactly how SDL sees the
virtual pad), so emulators using gamecontrollerdb resolve face buttons/triggers
the same way Steam does.
"""
from __future__ import annotations

import argparse
import ctypes
from pathlib import Path

GAMECONTROLLERDB_PATHS = [
    Path.home() / "Applications/gamecontrollerdb.txt",
    Path.home() / ".config/EmuDeck/backend/configs/gamecontrollerdb.txt",
]

# Only auto-write mappings for our virtual pads.
NGC_NAME_HINTS = ("nso gamecube", "pro controller 2", "joy-con 2")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="append ngc pad mappings to gamecontrollerdb files")
    args = ap.parse_args()

    import sdl2

    sdl2.SDL_SetHint(b"SDL_JOYSTICK_HIDAPI", b"0")  # see raw evdev pads, not HIDAPI
    sdl2.SDL_Init(sdl2.SDL_INIT_GAMECONTROLLER | sdl2.SDL_INIT_JOYSTICK)

    mappings: list[tuple[str, str]] = []  # (name, mapping line)
    n = sdl2.SDL_NumJoysticks()
    print(f"{n} joystick(s) detected\n")
    for i in range(n):
        name = sdl2.SDL_JoystickNameForIndex(i)
        name = name.decode() if name else f"joy{i}"
        guid = sdl2.SDL_JoystickGetDeviceGUID(i)
        buf = ctypes.create_string_buffer(33)
        sdl2.SDL_JoystickGetGUIDString(guid, buf, 33)
        guid_str = buf.value.decode()
        is_gc = bool(sdl2.SDL_IsGameController(i))
        mapping = None
        if is_gc:
            gc = sdl2.SDL_GameControllerOpen(i)
            if gc:
                m = sdl2.SDL_GameControllerMapping(gc)
                mapping = m.decode() if m else None
                sdl2.SDL_GameControllerClose(gc)
        print(f"[{i}] {name}")
        print(f"     GUID: {guid_str}  gamecontroller={is_gc}")
        if mapping:
            print(f"     mapping: {mapping}")
            if any(h in name.lower() for h in NGC_NAME_HINTS):
                mappings.append((name, mapping))
        print()

    if args.write and mappings:
        for path in GAMECONTROLLERDB_PATHS:
            if not path.parent.is_dir():
                continue
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
            lines = existing.splitlines()
            added = 0
            for name, mapping in mappings:
                guid = mapping.split(",", 1)[0]
                if any(line.startswith(guid + ",") for line in lines):
                    continue
                lines.append(mapping)
                added += 1
            if added:
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                print(f"appended {added} mapping(s) -> {path}")
            else:
                print(f"no new mappings for {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
