#!/usr/bin/env python3
"""Detect gamepads, match Steam player slots, map to per-device emulator profiles."""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

# Per-device MACs are optional and pin known controllers to a fixed player slot
# for extra reliability. Leave empty to rely on name-based classification, or set
# them to your controllers' MACs via these env vars (or by editing the defaults).
EXLENE_MAC = os.environ.get("BAZZITE_EXLENE_MAC", "").upper()
DUALSENSE_MAC = os.environ.get("BAZZITE_DUALSENSE_MAC", "").upper()
MCON_MAC = os.environ.get("BAZZITE_MCON_MAC", "").upper()
N64_MAC = os.environ.get("BAZZITE_N64_MAC", "").upper()

# Fallback Steam player slots (0-based) when preferences_*.vdf is missing.
# Player order: P1 NSO GameCube (virtual, kind-based below), P2 Exlene,
# P3 N64, P4 DualSense.
FIXED_STEAM_SLOTS = {
    mac: slot
    for mac, slot in ((EXLENE_MAC, 1), (N64_MAC, 2), (DUALSENSE_MAC, 3))
    if mac
}

# Deterministic player slots for the user's known controllers, keyed by kind.
# This takes precedence over Steam LED slots so the Dolphin GameCube port order
# stays fixed (P1 NSO GameCube, P2 Exlene, P3 N64, P4 DualSense). The NSO
# GameCube pad is virtual (no MAC), so it must be ordered by kind.
FIXED_KIND_SLOTS = {
    "gamecube_nso": 0,
    "exlene": 1,
    "n64_nso": 2,
    "dualsense": 3,
}

STEAM_PREFS_DIRS = [
    Path.home()
    / ".local/share/Steam/steamapps/common/Steam Controller Configs/921607934/config",
    Path.home() / ".local/share/Steam/config",
]

SKIP_NAME_PARTS = (
    "keyboard",
    "mouse",
    "led",
    "touchpad",
    "consumer",
    "mouse emulation",
    "motion sensors",
)

# Virtual pads created by Sunshine/Moonlight (not local Bluetooth/USB).
VIRTUAL_KINDS = frozenset({"steam_virtual", "stream_ds5", "moonlight_x360"})

PROFILE_BY_KIND = {
    "gamecube_nso": "GC_nso_gamecube",
    "switch2_pro": "GC_nintendo_layout",
    "exlene": "GC_exlene_bt",
    "dualsense": "GC_dualsense_bt",
    "stream_ds5": "GC_dualsense_bt",
    "mcon": "GC_xbox_layout",
    "n64_nso": "GC_mkdd_n64",
    "xbox": "GC_xbox_layout",
    "moonlight_x360": "GC_xbox_layout",
    "steam_virtual": "GC_xbox_layout",
    "generic": "GC_exlene_bt",
}

DOLPHIN_INI = (
    Path.home()
    / ".var/app/org.DolphinEmu.dolphin-emu/config/dolphin-emu/GCPadNew.ini"
)
PROF_DIR = DOLPHIN_INI.parent / "Profiles/GCPad"


@dataclass
class Pad:
    path: str
    name: str
    kind: str
    evdev_idx: int
    mac: str | None
    steam_slot: int | None  # 0 = Steam player 1
    profile: str
    device: str

    @property
    def steam_player(self) -> int:
        """1-based Steam player number for display."""
        return (self.steam_slot + 1) if self.steam_slot is not None else 0


def normalize_mac(mac: str) -> str:
    return mac.replace("-", ":").upper()


def mac_from_phys(phys: str) -> str | None:
    if not phys:
        return None
    m = re.search(r"([0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5})", phys)
    return normalize_mac(m.group(1)) if m else None


def classify(name: str) -> str | None:
    low = name.lower()
    if any(s in low for s in SKIP_NAME_PARTS):
        return None
    # DualSense exposes a separate motion-sensor evdev node; never treat as a pad.
    if "motion sensor" in low:
        return None
    # Switch 2 virtual pads from the BLE bridge (ngc). Match these BEFORE the
    # generic "pro controller" rule so the Pro Controller 2 is not mistaken for
    # the Exlene, and the NSO GameCube pad gets its native GameCube profile.
    if "gamecube" in low:
        return "gamecube_nso"
    if "pro controller 2" in low or "switch 2 pro" in low or "switch2 pro" in low:
        return "switch2_pro"
    if re.search(r"x-box 360 pad\s*(\d+)", low) or "xbox 360" in low:
        return "steam_virtual"
    if "steam virtual gamepad" in low:
        return "steam_virtual"
    if "dualsense" in low or "playstation 5" in low:
        return "dualsense"
    if "dualshock" in low or "ps4 controller" in low:
        return "mcon"
    # Local DS4/MCON in DS4 mode often appears as "Wireless Controller".
    # We classify it as PlayStation-style by default; stream conversion happens later.
    if "wireless controller" in low:
        return "mcon"
    if "sony interactive" in low or "computer entertainment" in low:
        return "mcon"
    if "ohsnap mcon" in low or "mcon iii" in low:
        return "xbox" if "xbox" in low else "mcon"
    if "mcon" in low:
        return "mcon"
    if "nintendo 64 controller" in low or "n64 controller" in low:
        return "n64_nso"
    if "n64" in low and "nintendo" in low:
        return "n64_nso"
    if "pro controller" in low or "switch pro" in low:
        return "exlene"
    if "xbox" in low or "x-box" in low:
        return "xbox"
    if "gamepad" in low or "controller" in low:
        return "generic"
    return None


def known_mac_kind(mac: str | None) -> str | None:
    if not mac:
        return None
    mac = normalize_mac(mac)
    if mac == EXLENE_MAC:
        return "exlene"
    if mac == DUALSENSE_MAC:
        return "dualsense"
    if mac == MCON_MAC:
        return "xbox"
    if mac == N64_MAC:
        return "n64_nso"
    return None


def fixed_steam_slot(mac: str | None) -> int | None:
    if not mac:
        return None
    return FIXED_STEAM_SLOTS.get(normalize_mac(mac))


def steam_prefs_candidates(mac: str, kind: str) -> list[Path]:
    clean = mac.replace(":", "").lower()
    prefixes: list[str] = []
    if kind == "exlene":
        prefixes = ["NSP", "NLP", "57e"]
    elif kind in ("dualsense", "stream_ds5"):
        prefixes = ["DS"]
    else:
        prefixes = ["DS", "NSP", "XBC", "XBO"]
    paths: list[Path] = []
    for base in STEAM_PREFS_DIRS:
        for prefix in prefixes:
            paths.append(base / f"preferences_{prefix}{clean}.vdf")
            paths.append(base / f"preferences_{prefix}{clean.upper()}.vdf")
    return paths


def read_steam_slot(mac: str | None, kind: str) -> int | None:
    if not mac:
        return None
    for path in steam_prefs_candidates(mac, kind):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'"player_slot_led"\s+"(\d+)"', text)
        if m:
            return int(m.group(1))
    return None


def virtual_pad_steam_slot(name: str) -> int | None:
    m = re.search(r"pad\s*(\d+)", name, re.I)
    if m:
        return int(m.group(1))
    return None


def in_stream_session() -> bool:
    for key in os.environ:
        if key.startswith("SUNSHINE_") or key.startswith("MOONLIGHT_"):
            return True
    if os.environ.get("BAZZITE_SUNSHINE_STREAM") in {"1", "true", "yes"}:
        return True
    return False


def in_steam_session() -> bool:
    if os.environ.get("SteamGameId") or os.environ.get("SteamAppId"):
        return True
    if os.environ.get("SteamClientLaunch") in {"1", "true", "yes"}:
        return True
    if os.environ.get("SteamEnv") or os.environ.get("STEAM_RUNTIME"):
        return True
    return False


def dolphin_backend() -> str:
    return os.environ.get("BAZZITE_DOLPHIN_BACKEND", "sdl").strip().lower()


def list_sdl_gamepads(*, include_steam_virtual: bool) -> list[dict]:
    try:
        import sdl2
    except ImportError:
        return []

    sdl2.SDL_Init(sdl2.SDL_INIT_GAMECONTROLLER | sdl2.SDL_INIT_JOYSTICK)
    pads: list[dict] = []
    for i in range(sdl2.SDL_NumJoysticks()):
        name = sdl2.SDL_JoystickNameForIndex(i).decode()
        kind = classify(name)
        if not kind:
            continue
        if kind == "steam_virtual" and not include_steam_virtual:
            continue
        pads.append(
            {
                "path": f"sdl:{i}",
                "name": name.strip(),
                "kind": kind,
                "mac": None,
                "phys": "",
                "evdev_idx": i,
            }
        )
    return pads


def list_gamepads(*, include_steam_virtual: bool) -> list[dict]:
    try:
        import evdev
    except ImportError:
        return []

    pads: list[dict] = []
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            d = evdev.InputDevice(path)
            if evdev.ecodes.EV_ABS not in d.capabilities():
                continue
            kind = classify(d.name)
            if not kind:
                continue
            if kind == "steam_virtual" and not include_steam_virtual:
                continue
            mac = mac_from_phys(d.phys or "")
            pads.append(
                {
                    "path": path,
                    "name": d.name.strip(),
                    "kind": kind,
                    "mac": mac,
                    "phys": d.phys or "",
                }
            )
        except (OSError, PermissionError):
            continue

    for i, p in enumerate(pads):
        p["evdev_idx"] = i
    return pads


def enrich_pad(raw: dict) -> Pad:
    mac = raw.get("mac")
    kind = known_mac_kind(mac) or raw["kind"]

    if kind in {"dualsense", "mcon"} and in_stream_session() and not known_mac_kind(mac):
        # Virtual Sunshine DualSense (uhid) — not the paired local DualSense.
        kind = "stream_ds5"

    if kind == "steam_virtual":
        slot = virtual_pad_steam_slot(raw["name"])
        kind = "moonlight_x360" if in_stream_session() else "steam_virtual"
    elif kind == "stream_ds5":
        slot = virtual_pad_steam_slot(raw["name"])
        if slot is None:
            slot = 0
    else:
        # Known controllers get a deterministic, fixed player order regardless of
        # Steam LED slots; everything else falls back to Steam prefs / slot order.
        slot = FIXED_KIND_SLOTS.get(kind)
        if slot is None:
            slot = read_steam_slot(mac, kind) or fixed_steam_slot(mac)
        if slot is None and kind in {"mcon", "n64_nso", "xbox"}:
            # Fallback to Steam-assigned slot order when no MAC-based prefs exist.
            slot = virtual_pad_steam_slot(raw["name"])

    profile = PROFILE_BY_KIND.get(kind, PROFILE_BY_KIND["generic"])
    idx = raw["evdev_idx"]
    if dolphin_backend() == "sdl" or raw["path"].startswith("sdl:"):
        device = f"SDL/{idx}/{raw['name']}"
    else:
        device = f"evdev/{idx}/{raw['name']}"
    return Pad(
        path=raw["path"],
        name=raw["name"],
        kind=kind,
        evdev_idx=raw["evdev_idx"],
        mac=mac,
        steam_slot=slot,
        profile=profile,
        device=device,
    )


def order_pads(pads: list[Pad]) -> list[Pad]:
    priority = {
        "gamecube_nso": 0,
        "exlene": 1,
        "n64_nso": 2,
        "dualsense": 3,
        "switch2_pro": 4,
        "mcon": 5,
        "xbox": 6,
    }

    def sort_key(p: Pad) -> tuple:
        slot = p.steam_slot if p.steam_slot is not None else 99
        kind_rank = priority.get(p.kind, 50)
        return (slot, kind_rank, p.evdev_idx)

    return sorted(pads, key=sort_key)


def finalize_device_indices(pads: list[Pad]) -> list[Pad]:
    """Set per-pad device strings for Dolphin and evdev_idx for RetroArch.

    Dolphin's SDL/N/DeviceName format uses N as the Nth occurrence of that
    specific device name (not the global sorted position).  Two DualSenses would
    be SDL/0 and SDL/1; a lone N64 controller is always SDL/0 regardless of its
    position in the sorted list.  evdev_idx keeps the sorted position so that
    RetroArch's joypad index assignment remains stable.
    """
    backend = dolphin_backend()
    out: list[Pad] = []
    name_count: dict[str, int] = {}
    for i, p in enumerate(pads):
        prefix = "SDL" if backend == "sdl" or p.path.startswith("sdl:") else "evdev"
        n = name_count.get(p.name, 0)
        name_count[p.name] = n + 1
        out.append(replace(p, evdev_idx=i, device=f"{prefix}/{n}/{p.name}"))
    return out


def detect_pads() -> list[Pad]:
    mode = os.environ.get("BAZZITE_INCLUDE_VIRTUAL_XBOX", "auto").strip().lower()
    stream_pad = os.environ.get("BAZZITE_STREAM_GAMEPAD", "ds5").strip().lower()

    backend = dolphin_backend()
    if backend == "sdl":
        all_raw = list_sdl_gamepads(include_steam_virtual=True)
        if not all_raw:
            # Headless shells may not enumerate SDL pads; fall back to evdev.
            all_raw = list_gamepads(include_steam_virtual=True)
    else:
        all_raw = list_gamepads(include_steam_virtual=True)
    enriched = [enrich_pad(p) for p in all_raw]
    physical = [p for p in enriched if p.kind not in VIRTUAL_KINDS]
    virtual = [p for p in enriched if p.kind in VIRTUAL_KINDS]

    if stream_pad in {"ds5", "ds4", "dualsense"}:
        ds5_virtual = [p for p in virtual if p.kind in {"stream_ds5", "dualsense"}]
        virtual = ds5_virtual
    elif stream_pad in {"x360", "xbox"}:
        virtual = [p for p in virtual if p.kind in {"moonlight_x360", "steam_virtual"}]

    if in_stream_session() and virtual:
        ds5 = [p for p in virtual if p.kind == "stream_ds5"]
        x360 = [p for p in virtual if p.kind in {"moonlight_x360", "steam_virtual"}]
        virtual = ds5 + x360 if ds5 else virtual

    steam_fallback = os.environ.get("BAZZITE_STEAM_INPUT_FALLBACK", "auto").strip().lower()
    use_steam_virtual = steam_fallback in {"always", "1", "true", "yes"}
    # In Steam sessions, prefer Steam virtual pads whenever present so
    # emulator-side port bindings match Steam player slots/LED routing.
    if steam_fallback == "auto" and in_steam_session() and virtual:
        use_steam_virtual = True

    if mode == "never":
        merged = physical
    elif mode in {"always", "1", "true", "yes"}:
        merged = physical + virtual
    elif in_stream_session():
        merged = physical + virtual
    elif use_steam_virtual and virtual:
        merged = virtual
    elif physical:
        merged = physical
    else:
        merged = virtual

    # Keep controllers distinct by device path so two same-model pads
    # (common in local multiplayer) do not collapse into one slot.
    dedup: dict[str, Pad] = {}
    for p in order_pads(merged):
        if p.path not in dedup:
            dedup[p.path] = p
    pads = finalize_device_indices(list(dedup.values()))
    # Normalize stale Steam LED slots to a local 0-based range.
    slots = [p.steam_slot for p in pads if p.steam_slot is not None]
    if slots and 0 not in slots:
        base = min(slots)
        pads = [
            replace(p, steam_slot=(p.steam_slot - base) if p.steam_slot is not None else None)
            for p in pads
        ]
    # Single-pad fallback when Steam slot metadata is unavailable.
    if len(pads) == 1 and pads[0].steam_slot is None:
        pads[0] = replace(pads[0], steam_slot=0)
    return pads


def load_profile_mapping(profile: str) -> dict[str, str]:
    prof = PROF_DIR / f"{profile}.ini"
    if not prof.is_file():
        prof = PROF_DIR / "GC_exlene_bt.ini"
    mapping: dict[str, str] = {}
    for line in prof.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("[Profile]"):
            continue
        if line.startswith("Device ="):
            continue
        k, _, v = line.partition(" = ")
        if k:
            mapping[k] = v
    return mapping


def gcpad_block(port: int, pad: Pad | None) -> str:
    if pad is None:
        return f"[GCPad{port}]\nDevice = \n"
    mapping = load_profile_mapping(pad.profile)
    lines = [f"[GCPad{port}]", f"Device = {pad.device}"]
    lines.extend(f"{k} = {v}" for k, v in mapping.items())
    return "\n".join(lines) + "\n"


def apply_dolphin(pads: list[Pad], max_ports: int = 4) -> list[str]:
    if not DOLPHIN_INI.is_file():
        raise SystemExit(f"Missing {DOLPHIN_INI}")

    original = DOLPHIN_INI.read_text(encoding="utf-8")
    logs: list[str] = []
    new_blocks: list[str] = []

    for port in range(1, max_ports + 1):
        pad = pads[port - 1] if port - 1 < len(pads) else None
        new_blocks.append(gcpad_block(port, pad).rstrip("\n"))
        if pad:
            labels = {
                "gamecube_nso": "NSO GameCube",
                "switch2_pro": "Switch 2 Pro",
                "exlene": "EXLENE",
                "dualsense": "DualSense",
                "stream_ds5": "Moonlight DS5",
                "moonlight_x360": "Moonlight X360",
            }
            label = labels.get(pad.kind, pad.kind.upper())
            logs.append(
                f"GCPad{port} (Steam P{pad.steam_player or '?'}) <- {label} "
                f"[{pad.profile}] {pad.device}"
            )
        else:
            logs.append(f"GCPad{port} <- (no pad)")

    text = "\n".join(new_blocks) + "\n"
    if text != original:
        backup = DOLPHIN_INI.with_suffix(f".ini.bak.{datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(DOLPHIN_INI, backup)
        DOLPHIN_INI.write_text(text, encoding="utf-8")
        logs.append(f"Wrote {DOLPHIN_INI.name}")
    return logs


def write_retroarch_overlay(pads: list[Pad], out: Path, max_players: int = 4) -> list[str]:
    # NSO N64 rumble is reliable through hid-nintendo (udev); SDL HIDAPI can miss FF.
    use_udev = any(p.kind == "n64_nso" for p in pads) and not any(
        p.kind in VIRTUAL_KINDS for p in pads
    )
    driver = "udev" if use_udev else "sdl2"
    lines = [
        "# Generated by bazzite-controller-detect.py — Steam / Moonlight player order",
        "input_autodetect_enable = \"true\"",
        f"input_joypad_driver = \"{driver}\"",
        "input_enable_rumble = \"true\"",
        "input_rumble_gain = \"100\"",
    ]
    logs: list[str] = []
    for i in range(max_players):
        player = i + 1
        if i < len(pads):
            idx = pads[i].evdev_idx
            lines.append(f'input_player{player}_joypad_index = "{idx}"')
            logs.append(f"RetroArch player{player} <- {pads[i].name} (index {idx})")
        else:
            lines.append(f'input_player{player}_joypad_index = "-1"')
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logs.append(f"Wrote {out}")
    return logs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="Print detected pads as JSON")
    ap.add_argument("--dolphin", action="store_true", help="Apply GCPad1-4 in Dolphin")
    ap.add_argument("--retroarch", metavar="OUT", help="Write RetroArch appendconfig")
    ap.add_argument("--max-players", type=int, default=4)
    args = ap.parse_args()

    pads = detect_pads()
    if args.json:
        print(json.dumps([asdict(p) for p in pads], indent=2))
        return 0

    if not args.dolphin and not args.retroarch:
        args.dolphin = True

    if args.dolphin:
        for line in apply_dolphin(pads, args.max_players):
            print(line)

    if args.retroarch:
        for line in write_retroarch_overlay(pads, Path(args.retroarch), args.max_players):
            print(line)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
