#!/usr/bin/env python3
"""Print SDL GUIDs + GameController mappings for connected pads, and optionally
upsert ngc mappings into ~/Applications/gamecontrollerdb.txt.

Run this ON the Bazzite box with the controllers awake/connected:
    python3 tools/sdl_guid.py            # list pads + GUID + mapping
    python3 tools/sdl_guid.py --write     # upsert ngc lines in Applications DB
    python3 tools/sdl_guid.py --restore-dbs  # fix truncated Steam/EmuDeck overrides

Never write a one-line override into ~/.local/share/Steam/gamecontrollerdb.txt —
SDL treats that file as the full mapping table and every other controller stops
working. Use --all-paths only if those files already contain a full DB (2000+ lines).
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
from pathlib import Path

GAMECONTROLLERDB_SEED = Path.home() / "Applications/gamecontrollerdb.txt"
# Only maintain the shared EmuDeck/Bazzite DB by default. Never write a tiny
# override into Steam's config — that replaces SDL's full mapping table.
GAMECONTROLLERDB_PATHS = [
    GAMECONTROLLERDB_SEED,
]
GAMECONTROLLERDB_OPTIONAL_PATHS = [
    Path.home() / ".config/EmuDeck/backend/configs/gamecontrollerdb.txt",
    Path.home() / ".local/share/Steam/gamecontrollerdb.txt",
]
MIN_DB_LINES = 64  # fewer mappings => truncated override, re-seed before write

def _mapping_lines(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def seed_mapping_lines(existing: list[str]) -> list[str]:
    """Recover from a truncated gamecontrollerdb (e.g. a single ngc line)."""
    if len(existing) >= MIN_DB_LINES:
        return existing
    if not GAMECONTROLLERDB_SEED.is_file():
        return existing
    seeded = _mapping_lines(GAMECONTROLLERDB_SEED.read_text(encoding="utf-8"))
    if len(seeded) >= MIN_DB_LINES:
        print(
            f"  re-seeded from {GAMECONTROLLERDB_SEED} ({len(seeded)} mappings)",
            file=sys.stderr,
        )
        return seeded
    return existing


def upsert_mapping(lines: list[str], mapping: str) -> list[str]:
    guid = mapping.split(",", 1)[0]
    kept = [line for line in lines if not line.startswith(guid + ",")]
    kept.append(mapping)
    return kept


def write_mappings(paths: list[Path], mappings: list[tuple[str, str]]) -> None:
    for path in paths:
        if not path.parent.is_dir():
            continue
        existing = (
            _mapping_lines(path.read_text(encoding="utf-8"))
            if path.is_file()
            else []
        )
        lines = seed_mapping_lines(existing)
        before = len(lines)
        for _name, mapping in mappings:
            lines = upsert_mapping(lines, mapping)
        if len(lines) < MIN_DB_LINES and path in GAMECONTROLLERDB_OPTIONAL_PATHS:
            print(
                f"skip {path} (would stay truncated; delete it or use --restore-dbs)",
                file=sys.stderr,
            )
            continue
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        added = len(mappings)
        print(f"updated {added} ngc mapping(s) -> {path} ({before} -> {len(lines)} lines)")


def restore_broken_dbs() -> int:
    """Fix Steam/EmuDeck overrides that were accidentally truncated to one ngc line."""
    if not GAMECONTROLLERDB_SEED.is_file():
        print(f"missing seed DB: {GAMECONTROLLERDB_SEED}", file=sys.stderr)
        return 1
    seed = _mapping_lines(GAMECONTROLLERDB_SEED.read_text(encoding="utf-8"))
    if len(seed) < MIN_DB_LINES:
        print(f"seed DB too small ({len(seed)} lines); not restoring", file=sys.stderr)
        return 1
    restored = 0
    for path in GAMECONTROLLERDB_OPTIONAL_PATHS:
        if not path.is_file():
            continue
        lines = _mapping_lines(path.read_text(encoding="utf-8"))
        if len(lines) >= MIN_DB_LINES:
            continue
        if len(lines) <= 2:
            path.unlink()
            print(f"removed truncated override {path} (Steam/SDL will use full DB again)")
            restored += 1
            continue
        path.write_text("\n".join(seed) + "\n", encoding="utf-8")
        print(f"restored {path} from seed ({len(seed)} lines)")
        restored += 1
    return 0 if restored else 0


# Only auto-write mappings for our virtual pads.
NGC_NAME_HINTS = ("nso gamecube", "pro controller 2", "joy-con 2")


# D-pad hat tokens may be copied from SDL's auto-generated line.
_KEEP_HAT_KEYS = frozenset({"dpup", "dpdown", "dpleft", "dpright"})

# ngc uinput exposes axes in Linux evdev code order (ABS_X..ABS_RZ), not the
# semantic a2/a3 right-stick layout. L trigger sits at a2, C-stick at a3/a4.
NGC_GC_AXES = (
    "leftx:a0",
    "lefty:a1",
    "lefttrigger:a2",
    "rightx:a3",
    "righty:a4",
    "righttrigger:a5",
    "platform:Linux",
)


def fix_gamecube_mapping(mapping: str) -> str:
    """Build a complete gamecontrollerdb line for ngc GameCube uinput pads.

    Matches ngc/gamepad.py button indices. Aligns with SDL's Switch 2 GameCube
    layout: Z+R click=right bumper, ZL+L=left bumper, back=C (minus)."""
    parts = mapping.split(",")
    if len(parts) < 2:
        return mapping
    guid, name = parts[0], parts[1]
    hat_tokens = [
        token for token in parts[2:]
        if token.split(":", 1)[0] in _KEEP_HAT_KEYS
    ]
    axis_tokens = list(NGC_GC_AXES[:-1]) + hat_tokens + [NGC_GC_AXES[-1]]
    # b0=A b1=B b2=X(NORTH) b3=Y(WEST) b4=Capture b5=L+ZL b6=R+Z b7=C b8=Start b9=Home
    buttons = [
        "a:b0",
        "b:b1",
        "x:b2",
        "y:b3",
        "back:b7",
        "start:b8",
        "guide:b9",
        "misc2:b4",
        "leftshoulder:b5",
        "rightshoulder:b6",
        "leftstick:b10",
        "rightstick:b11",
        "hint:SDL_GAMECONTROLLER_USE_GAMECUBE_LABELS:=1",
    ]
    return ",".join([guid, name, *buttons, *axis_tokens])


# ngc uinput Pro / Joy-Con 2: axes in Linux evdev order (ABS_X..ABS_RZ).
NGC_PRO_AXES = (
    "leftx:a0",
    "lefty:a1",
    "rightx:a2",
    "righty:a3",
    "lefttrigger:a4",
    "righttrigger:a5",
    "platform:Linux",
)


def fix_pro_mapping(mapping: str) -> str:
    """Build gamecontrollerdb line for ngc Pro / Joy-Con 2 uinput pads.

    Face buttons use semantic Nintendo positions (A=SOUTH, B=EAST, …) per
    ngc/gamepad.py PRO_BUTTON_MAP sorted evdev key order."""
    parts = mapping.split(",")
    if len(parts) < 2:
        return mapping
    guid, name = parts[0], parts[1]
    hat_tokens = [
        token for token in parts[2:]
        if token.split(":", 1)[0] in _KEEP_HAT_KEYS
    ]
    # b0=A b1=B b2=Y b3=X b4=Capture b5=L b6=R b7=ZL b8=ZR b9=Minus b10=Plus b11=Home
    buttons = [
        "a:b0",
        "b:b1",
        "x:b3",
        "y:b2",
        "back:b9",
        "start:b10",
        "guide:b11",
        "misc1:b4",
        "leftshoulder:b5",
        "rightshoulder:b6",
        "leftstick:b12",
        "rightstick:b13",
        "hint:SDL_GAMECONTROLLER_USE_BUTTON_LABELS:=1",
    ]
    axis_tokens = list(NGC_PRO_AXES[:-1]) + hat_tokens + [NGC_PRO_AXES[-1]]
    return ",".join([guid, name, *buttons, *axis_tokens])


def mapping_for_pad(name: str, mapping: str | None) -> str | None:
    if not mapping:
        return None
    low = name.lower()
    if "gamecube" in low:
        return fix_gamecube_mapping(mapping)
    if "pro controller 2" in low or "joy-con 2" in low:
        return fix_pro_mapping(mapping)
    return mapping


def ngc_status_hint() -> None:
    """Print why ngc pads may be missing from SDL."""
    try:
        from ngc.status import read_state
    except ImportError:
        return
    try:
        state = read_state()
    except Exception:
        return
    pending = [c for c in state.controllers if not c.connected]
    if not pending:
        return
    names = ", ".join(c.name or c.mac for c in pending)
    print(
        "Note: ngc has saved controllers that are not connected yet "
        f"({names}).\n"
        "      Press any button (or Sync briefly) on each pad, then re-run.\n"
        "      Watch: journalctl --user -u nso-gc.service -f\n"
        "      until you see \"virtual gamepad ready\".\n",
        file=sys.stderr,
    )


def wait_for_ngc_pads(seconds: float) -> None:
    """Poll SDL until an ngc virtual pad appears or timeout."""
    import sdl2

    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        sdl2.SDL_Quit()
        sdl2.SDL_SetHint(b"SDL_JOYSTICK_HIDAPI", b"0")
        sdl2.SDL_Init(sdl2.SDL_INIT_GAMECONTROLLER | sdl2.SDL_INIT_JOYSTICK)
        for i in range(sdl2.SDL_NumJoysticks()):
            name = sdl2.SDL_JoystickNameForIndex(i)
            name = name.decode() if name else ""
            if any(h in name.lower() for h in NGC_NAME_HINTS):
                return
        time.sleep(0.5)
    print(f"Timed out after {seconds:.0f}s waiting for ngc pads.", file=sys.stderr)
    ngc_status_hint()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="upsert ngc pad mappings into Applications/gamecontrollerdb.txt")
    ap.add_argument("--all-paths", action="store_true",
                    help="also write EmuDeck + Steam paths (re-seeds if truncated)")
    ap.add_argument("--restore-dbs", action="store_true",
                    help="remove/fix truncated Steam/EmuDeck gamecontrollerdb overrides")
    ap.add_argument("--wait", type=float, metavar="SEC", default=0,
                    help="wait up to SEC seconds for ngc virtual pads before scanning")
    args = ap.parse_args()

    if args.restore_dbs:
        return restore_broken_dbs()

    if args.wait > 0:
        wait_for_ngc_pads(args.wait)

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
            mapping = mapping_for_pad(name, mapping)
            print(f"     mapping: {mapping}")
            if any(h in name.lower() for h in NGC_NAME_HINTS):
                mappings.append((name, mapping))
        print()

    if args.write and mappings:
        paths = list(GAMECONTROLLERDB_PATHS)
        if args.all_paths:
            paths.extend(GAMECONTROLLERDB_OPTIONAL_PATHS)
        write_mappings(paths, mappings)
    elif args.write:
        print("No ngc pad mappings written (Switch 2 virtual pads not in SDL).", file=sys.stderr)
        ngc_status_hint()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
