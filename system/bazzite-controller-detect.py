#!/usr/bin/env python3
"""Detect gamepads, match Steam player slots, map to per-device emulator profiles."""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Optional

# Per-device MACs are optional and pin known controllers to a fixed player slot
# for extra reliability. Leave empty to rely on name-based classification, or set
# them to your controllers' MACs via these env vars (or by editing the defaults).
EXLENE_MAC = os.environ.get("BAZZITE_EXLENE_MAC", "98:B6:E9:14:F3:2B").upper()
DUALSENSE_MAC = os.environ.get("BAZZITE_DUALSENSE_MAC", "4C:B9:9B:1D:85:F7").upper()
MCON_MAC = os.environ.get("BAZZITE_MCON_MAC", "D2:D0:8C:8A:40:4E").upper()
N64_MAC = os.environ.get("BAZZITE_N64_MAC", "DC:CD:18:62:AB:C8").upper()

# Fallback Steam player slots (0-based) when preferences_*.vdf is missing.
# Dolphin layout: P1 GC, P2 EXLENE, P3 Switch 2 Pro, P4 DualSense.
FIXED_STEAM_SLOTS = {
    mac: slot
    for mac, slot in (
        (EXLENE_MAC, 1),
        (DUALSENSE_MAC, 3),
        (MCON_MAC, 4),
        (N64_MAC, 5),
    )
    if mac
}

FIXED_KIND_SLOTS = {
    "gamecube_nso": 0,
    "exlene": 1,
    "switch2_pro": 2,
    "dualsense": 3,
    "mcon": 4,
    "n64_nso": 5,
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

# Virtual pads created by Sunshine/Moonlight / Steam Remote Play (not local BT/USB).
VIRTUAL_KINDS = frozenset({"steam_virtual", "stream_ds5", "moonlight_x360"})
# Steam Link / Moonlight Xbox pads — take P1 when present, else ignore.
REMOTE_XBOX_KINDS = frozenset({"steam_virtual", "moonlight_x360"})

PROFILE_BY_KIND = {
    "gamecube_nso": "GC_nso_gamecube",
    "switch2_pro": "GC_switch2_pro_bt",
    "joycon2": "GC_switch2_pro_bt",
    "joycon2_left": "GC_switch2_pro_bt",
    "joycon2_right": "GC_switch2_pro_bt",
    "exlene": "GC_exlene_bt",
    "dualsense": "GC_dualsense_bt",
    "stream_ds5": "GC_dualsense_bt",
    "mcon": "GC_mcon_bt",
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
    sdl_guid: str | None = None  # live SDL GUID (matches what emulators see)
    sdl_idx: int | None = None   # raw SDL enumeration index (RetroArch sdl2 driver)
    udev_idx: int | None = None  # raw joystick-node order (RetroArch udev driver)

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
    if "joy-con 2" in low or "joycon 2" in low:
        if "right" in low:
            return "joycon2_right"
        if "left" in low:
            return "joycon2_left"
        return "joycon2"
    if "bazzite link pad" in low:
        return "steam_virtual"
    if re.search(r"x-box 360 pad\s*(\d+)", low) or "xbox 360" in low:
        return "steam_virtual"
    if "xbox series x controller" in low or "steam virtual gamepad" in low:
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



GEMMA_ACCOUNT_ID = 708606858
# ngc bridge pads expose Nintendo diamond face buttons (A=SOUTH, B=EAST, …).
NINTENDO_LAYOUT_KINDS = frozenset({
    "switch2_pro",
    "joycon2",
    "joycon2_left",
    "joycon2_right",
    "gamecube_nso",
    "n64_nso",
})


def active_steam_account_id() -> int | None:
    """Best-effort active Steam userdata id (e.g. Gemma 708606858)."""
    helper = Path.home() / ".local/bin/get-active-steam-user.sh"
    if helper.is_file():
        try:
            out = subprocess.check_output([str(helper)], text=True, timeout=3)
            for line in out.splitlines():
                if line.startswith("account_id="):
                    return int(line.split("=", 1)[1].strip())
        except (subprocess.SubprocessError, ValueError, OSError):
            pass
    return None


def gemma_physical_pad_order_enabled() -> bool:
    """Gemma Steam profile: Exlene P1 if present, else DualSense P1 (USB/VH).

    Env BAZZITE_GEMMA_DS_ORDER=always|never|auto (default auto = when Gemma active).
    """
    mode = os.environ.get("BAZZITE_GEMMA_DS_ORDER", "auto").strip().lower()
    if mode in {"never", "0", "false", "no"}:
        return False
    if mode in {"always", "1", "true", "yes", "force"}:
        return True
    return active_steam_account_id() == GEMMA_ACCOUNT_ID


def want_remote_xbox_p1() -> bool:
    """Link/X360 takes P1 only when explicitly wanted or a live stream is up.

    Local projector play (Switch 2 / NSO pads) must not lose P1 to a leftover
    Sunshine X360 device after Gemma's Steam Link session ends.

    When Gemma is active with DualSense (VH USB) and/or Exlene, those physical
    pads own P1/P2 — do not let Sunshine x360 shells steal the slots.
    """
    prefer = os.environ.get("BAZZITE_REMOTE_XBOX_P1", "auto").strip().lower()
    if prefer in {"never", "0", "false", "no"}:
        return False
    if prefer in {"always", "1", "true", "yes", "force"}:
        return True
    # Gemma + physical DualSense/Exlene: checked later in detect_pads once pads known.
    # auto: only during an active stream / explicit remote-play env
    if os.environ.get("BAZZITE_STEAM_REMOTE_PLAY", "").strip().lower() in {
        "1", "true", "yes",
    }:
        return True
    return in_stream_session()

def in_stream_session() -> bool:
    """True only for a live Moonlight/Sunshine session (not leftover virtual pads)."""
    for key in os.environ:
        if key.startswith("SUNSHINE_") or key.startswith("MOONLIGHT_"):
            return True
    if os.environ.get("BAZZITE_SUNSHINE_STREAM") in {"1", "true", "yes"}:
        return True
    # Set by sunshine-stream-prep.sh for the duration of an active client stream.
    flag = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / (
        "bazzite-sunshine-stream-active"
    )
    if flag.exists():
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


def remote_play_xbox_js_nodes() -> list[str]:
    """Host-side Steam Remote Play / Steam Link Xbox pads (uinput x360)."""
    import array
    import fcntl

    def js_name(path: str) -> str:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            # chmod 000 (e.g. hidden ASRock LED js0) — fall back to sysfs name
            try:
                sysfs = Path("/sys/class/input") / Path(path).name / "device" / "name"
                return sysfs.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return ""
        try:
            buf = array.array("B", [0] * 128)
            # JSIOCGNAME(len) = _IOC(_IOC_READ, 'j', 0x13, len)
            ioc = (2 << 30) | (128 << 16) | (ord("j") << 8) | 0x13
            fcntl.ioctl(fd, ioc, buf)
            return bytes(buf).split(b"\0", 1)[0].decode("utf-8", "replace")
        except OSError:
            try:
                sysfs = Path("/sys/class/input") / Path(path).name / "device" / "name"
                return sysfs.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return ""
        finally:
            os.close(fd)

    nodes: list[str] = []
    for path in sorted(glob.glob("/dev/input/js*")):
        name = js_name(path)
        if re.search(r"x-box 360 pad\s*\d+", name, re.I) or re.search(
            r"steam virtual gamepad", name, re.I
        ):
            nodes.append(path)
    return nodes


def in_steam_remote_play() -> bool:
    """True when a Steam Link / Remote Play Xbox pad is attached on the host."""
    if os.environ.get("BAZZITE_STEAM_REMOTE_PLAY") in {"1", "true", "yes"}:
        return True
    if remote_play_xbox_js_nodes():
        return True
    try:
        text = Path("/proc/bus/input/devices").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(re.search(r'Name="Microsoft X-Box 360 pad\s*\d+"', text, re.I))


def ensure_sdl_sees_remote_xbox() -> None:
    """SDL udev enum sometimes misses uinput x360 pads; pin them via env hint."""
    nodes = remote_play_xbox_js_nodes()
    if not nodes:
        return
    existing = [
        p
        for p in os.environ.get("SDL_JOYSTICK_DEVICE", "").split(":")
        if p.strip()
    ]
    merged = existing[:]
    for n in nodes:
        if n not in merged:
            merged.append(n)
    os.environ["SDL_JOYSTICK_DEVICE"] = ":".join(merged)


def dolphin_backend() -> str:
    return os.environ.get("BAZZITE_DOLPHIN_BACKEND", "sdl").strip().lower()


def list_sdl_gamepads(*, include_steam_virtual: bool) -> list[dict]:
    try:
        import sdl2
    except ImportError:
        return []

    import ctypes

    if include_steam_virtual:
        ensure_sdl_sees_remote_xbox()

    sdl2.SDL_Init(sdl2.SDL_INIT_GAMECONTROLLER | sdl2.SDL_INIT_JOYSTICK)
    pads: list[dict] = []
    for i in range(sdl2.SDL_NumJoysticks()):
        name = sdl2.SDL_JoystickNameForIndex(i).decode()
        kind = classify(name)
        if not kind:
            continue
        if kind == "steam_virtual" and not include_steam_virtual:
            continue
        guid = sdl2.SDL_JoystickGetDeviceGUID(i)
        buf = ctypes.create_string_buffer(33)
        sdl2.SDL_JoystickGetGUIDString(guid, buf, 33)
        pads.append(
            {
                "path": f"sdl:{i}",
                "name": name.strip(),
                "kind": kind,
                "mac": None,
                "phys": "",
                "evdev_idx": i,
                "sdl_guid": buf.value.decode("ascii", errors="ignore").strip().lower(),
                "sdl_idx": i,
            }
        )
    return pads


def list_gamepads(*, include_steam_virtual: bool) -> list[dict]:
    try:
        import evdev
    except ImportError:
        return []

    def event_num(path: str) -> int:
        m = re.search(r"event(\d+)$", path)
        return int(m.group(1)) if m else 0

    pads: list[dict] = []
    # udev-driver index space: every joystick-capable node in numeric order,
    # including ones we skip (e.g. the ASRock LED "joystick") — RetroArch's udev
    # driver counts those too, so player indices must account for them.
    joystick_order = 0
    for path in sorted(glob.glob("/dev/input/event*"), key=event_num):
        try:
            d = evdev.InputDevice(path)
            caps = d.capabilities()
            if evdev.ecodes.EV_ABS not in caps:
                continue
            keys = set(caps.get(evdev.ecodes.EV_KEY, []))
            is_joystick = any(0x120 <= k <= 0x14F for k in keys)
            if not is_joystick:
                continue
            my_udev_idx = joystick_order
            joystick_order += 1
            kind = classify(d.name)
            if not kind:
                continue
            if kind == "steam_virtual" and not include_steam_virtual:
                continue
            # BT devices: `uniq` is the controller's own MAC; `phys` is only
            # the host adapter's MAC (identical for every BT pad).
            mac = mac_from_phys(d.uniq or "") or mac_from_phys(d.phys or "")
            pads.append(
                {
                    "path": path,
                    "name": d.name.strip(),
                    "kind": kind,
                    "mac": mac,
                    "phys": d.phys or "",
                    "udev_idx": my_udev_idx,
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
    if raw["path"].startswith("/dev/"):
        device = f"evdev/{idx}/{raw['name']}"
    elif dolphin_backend() == "sdl" or raw["path"].startswith("sdl:"):
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
        sdl_guid=raw.get("sdl_guid"),
        sdl_idx=raw.get("sdl_idx"),
        udev_idx=raw.get("udev_idx"),
    )


def order_pads(pads: list[Pad], *, gemma_physical: bool = False) -> list[Pad]:
    # Dolphin / global: Remote Play Xbox first when present, then
    # P1 GC, P2 EXLENE, P3 Switch 2 Pro, P4 DualSense.
    #
    # Gemma Steam profile + DualSense (USB/VH) and/or Exlene:
    #   Exlene → P1 if present, DualSense → P1 alone or P2 with Exlene.
    #   Sunshine/Link x360 shells sort after physical pads.
    if gemma_physical:
        priority = {
            "exlene": 0,
            "dualsense": 1,
            "stream_ds5": 1,
            "gamecube_nso": 2,
            "switch2_pro": 3,
            "joycon2": 3,
            "joycon2_left": 3,
            "joycon2_right": 3,
            "mcon": 4,
            "n64_nso": 5,
            "xbox": 6,
            "moonlight_x360": 90,
            "steam_virtual": 91,
        }
        remote_first = False
    else:
        priority = {
            "steam_virtual": -2,
            "moonlight_x360": -1,
            "gamecube_nso": 0,
            "exlene": 1,
            "switch2_pro": 2,
            "joycon2": 2,
            "joycon2_left": 2,
            "joycon2_right": 2,
            "dualsense": 3,
            "stream_ds5": 3,
            "mcon": 4,
            "n64_nso": 5,
            "xbox": 6,
        }
        remote_first = want_remote_xbox_p1()

    # Per-launcher boost, e.g. BAZZITE_PAD_PRIORITY="n64_nso" makes the NSO N64
    # pad player 1 for N64 cores even when the full game-night set is connected.
    # Remote Play Xbox still wins unless explicitly disabled.
    boost = [
        k.strip()
        for k in os.environ.get("BAZZITE_PAD_PRIORITY", "").split(",")
        if k.strip()
    ]
    if gemma_physical and not boost:
        boost = ["exlene", "dualsense"]

    def sort_key(p: Pad) -> tuple:
        if remote_first and p.kind in {"steam_virtual", "moonlight_x360"}:
            slot = p.steam_slot if p.steam_slot is not None else 0
            return (-1, slot, priority.get(p.kind, -1), p.evdev_idx)
        if p.kind in boost:
            return (0, boost.index(p.kind), 0, p.evdev_idx)
        if gemma_physical:
            # Ignore fixed Steam LED slots — Exlene/DualSense order is explicit.
            return (1, priority.get(p.kind, 50), p.evdev_idx)
        slot = p.steam_slot if p.steam_slot is not None else 99
        kind_rank = priority.get(p.kind, 50)
        return (1, slot, kind_rank, p.evdev_idx)

    return sorted(pads, key=sort_key)


def finalize_device_indices(pads: list[Pad]) -> list[Pad]:
    """Set per-pad device strings for Dolphin and evdev_idx for RetroArch.

    Dolphin's SDL/N/DeviceName format uses N as the Nth occurrence of that
    specific device name (not the global sorted position).  Two DualSenses would
    be SDL/0 and SDL/1; a lone N64 controller is always SDL/0 regardless of its
    position in the sorted list.  evdev_idx keeps the sorted position so that
    RetroArch's joypad index assignment remains stable.

    When BAZZITE_DOLPHIN_BACKEND=sdl, prefer SDL/ device strings whenever the pad
    is visible to SDL (sdl: path or sdl_guid). Profiles use SDL semantic names
    (SOUTH, Trigger L, …) which do not work on evdev/ bindings.
    """
    backend = dolphin_backend()
    out: list[Pad] = []
    name_count: dict[tuple[str, str], int] = {}
    for i, p in enumerate(pads):
        if backend == "sdl" and (
            p.path.startswith("sdl:") or p.sdl_guid or p.kind
            in {
                "gamecube_nso",
                "switch2_pro",
                "exlene",
                "dualsense",
                "stream_ds5",
                "n64_nso",
            }
        ):
            prefix = "SDL"
        elif p.path.startswith("/dev/"):
            prefix = "evdev"
        elif p.path.startswith("sdl:") or backend == "sdl":
            prefix = "SDL"
        else:
            prefix = "evdev"
        key = (prefix, p.name)
        n = name_count.get(key, 0)
        name_count[key] = n + 1
        out.append(replace(p, evdev_idx=i, device=f"{prefix}/{n}/{p.name}"))
    return out


def detect_pads() -> list[Pad]:
    mode = os.environ.get("BAZZITE_INCLUDE_VIRTUAL_XBOX", "auto").strip().lower()
    stream_pad = os.environ.get("BAZZITE_STREAM_GAMEPAD", "x360").strip().lower()
    remote_play = in_steam_remote_play() and want_remote_xbox_p1()
    # Steam Link USB Xbox → host "Microsoft X-Box 360 pad N". Prefer that
    # over Sunshine's default DS5 virtual preference when Remote Play is live.
    if remote_play and stream_pad in {"ds5", "ds4", "dualsense", "auto", ""}:
        if os.environ.get("BAZZITE_STREAM_GAMEPAD", "").strip() == "":
            stream_pad = "x360"

    backend = dolphin_backend()
    want_virtual = (mode == "always" or mode == "1" or mode == "true"
                   or (mode not in {"never", "0", "false", "no"} and want_remote_xbox_p1()))
    if backend == "sdl":
        all_raw = list_sdl_gamepads(include_steam_virtual=want_virtual)
        # Pads like OhSnap MCON often appear on evdev but not in SDL; keep them.
        def norm_name(n: str) -> str:
            # SDL prefixes kernel names ("N64 Controller" -> "Nintendo N64
            # Controller", "Pro Controller" -> "Nintendo Switch Pro Controller").
            words = [w for w in n.lower().split() if w not in ("nintendo", "switch")]
            return " ".join(words)

        claimed: set[int] = set()
        # Include steam_virtual from evdev too — SDL udev often misses uinput
        # x360 pads until SDL_JOYSTICK_DEVICE is set (handled above).
        for p in list_gamepads(include_steam_virtual=want_virtual):
            match = None
            for i, s in enumerate(all_raw):
                if i in claimed:
                    continue
                if norm_name(s["name"]) == norm_name(p["name"]):
                    match = i
                    break
            if match is None:
                # Pad visible on evdev only (e.g. OhSnap MCON) — keep it.
                all_raw.append(p)
            else:
                claimed.add(match)
                s = all_raw[match]
                if not s.get("mac"):
                    s["mac"] = p.get("mac")
                    s["phys"] = p.get("phys", "")
                if s.get("udev_idx") is None:
                    s["udev_idx"] = p.get("udev_idx")
        if not all_raw:
            all_raw = list_gamepads(include_steam_virtual=want_virtual)
    else:
        all_raw = list_gamepads(include_steam_virtual=want_virtual)
    enriched = [enrich_pad(p) for p in all_raw]
    physical = [p for p in enriched if p.kind not in VIRTUAL_KINDS]
    virtual = [p for p in enriched if p.kind in VIRTUAL_KINDS]

    if stream_pad in {"ds5", "ds4", "dualsense"} and not remote_play:
        ds5_virtual = [p for p in virtual if p.kind in {"stream_ds5", "dualsense"}]
        virtual = ds5_virtual
    elif stream_pad in {"x360", "xbox"} or remote_play:
        # Keep Remote Play / Moonlight Xbox pads; drop unrelated stream DS5 noise.
        x360 = [p for p in virtual if p.kind in {"moonlight_x360", "steam_virtual"}]
        if x360:
            virtual = x360

    if in_stream_session() and virtual and not remote_play:
        ds5 = [p for p in virtual if p.kind == "stream_ds5"]
        x360 = [p for p in virtual if p.kind in {"moonlight_x360", "steam_virtual"}]
        virtual = ds5 + x360 if ds5 else virtual

    steam_fallback = os.environ.get("BAZZITE_STEAM_INPUT_FALLBACK", "auto").strip().lower()
    use_steam_virtual = steam_fallback in {"always", "1", "true", "yes"}
    # In Steam sessions, prefer Steam virtual pads whenever present so
    # emulator-side port bindings match Steam player slots/LED routing.
    if steam_fallback == "auto" and in_steam_session() and virtual:
        use_steam_virtual = True

    gemma_kinds = {p.kind for p in physical}
    gemma_physical = gemma_physical_pad_order_enabled() and bool(
        gemma_kinds
        & ({"dualsense", "exlene", "stream_ds5"} | NINTENDO_LAYOUT_KINDS)
    )

    if mode == "never":
        merged = physical
    elif mode in {"always", "1", "true", "yes"}:
        merged = physical + virtual
    elif gemma_physical:
        # Gemma + DualSense (VH) / Exlene: emulators bind only those physical pads
        # so Sunshine x360 shells do not fill P2–P4.
        merged = physical
    elif remote_play or in_stream_session():
        # Remote Play / Sunshine: keep local pads, but include the stream pad
        # so order_pads can put the Link Xbox (or Moonlight pad) at P1.
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
    for p in order_pads(merged, gemma_physical=gemma_physical):
        if p.path not in dedup:
            dedup[p.path] = p
    pads = finalize_device_indices(list(dedup.values()))

    # Gemma layout: rewrite steam_slot LEDs to match Exlene→P1 / DualSense→P2.
    if gemma_physical:
        rewritten: list[Pad] = []
        for i, p in enumerate(pads):
            if p.kind in {"exlene", "dualsense", "stream_ds5"} or p.kind not in VIRTUAL_KINDS:
                rewritten.append(replace(p, steam_slot=i))
            else:
                rewritten.append(p)
        pads = rewritten
    # Normalize stale Steam LED slots only when no fixed kind/MAC layout is in use.
    uses_fixed_layout = any(
        p.kind in FIXED_KIND_SLOTS or known_mac_kind(p.mac) for p in pads
    )
    slots = [p.steam_slot for p in pads if p.steam_slot is not None]
    if slots and 0 not in slots and not uses_fixed_layout:
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


def parse_gcpad_blocks(text: str) -> dict[int, str]:
    blocks: dict[int, str] = {}
    current_port: int | None = None
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("[GCPad") and line.endswith("]"):
            if current_port is not None:
                blocks[current_port] = "\n".join(lines).rstrip("\n")
            try:
                current_port = int(line[6:-1])
            except ValueError:
                current_port = None
            lines = [line]
        elif current_port is not None:
            lines.append(line)
    if current_port is not None:
        blocks[current_port] = "\n".join(lines).rstrip("\n")
    return blocks


# Fixed Dolphin ports: P1 GC, P2 EXLENE, P3 Switch 2 Pro, P4 DualSense.
# Absolute slots (not compacted) — NSO GameCube is always GCPad1 when connected,
# unless a Steam Link / Remote Play Xbox pad is present (then that is GCPad1).
DOLPHIN_FIXED_SLOTS = (
    ("gamecube_nso",),
    ("exlene",),
    ("switch2_pro", "joycon2", "joycon2_left", "joycon2_right"),
    ("dualsense", "stream_ds5"),
)


def _pads_for_dolphin_slots(pads: list[Pad], max_ports: int = 4) -> list[Pad | None]:
    by_kind: dict[str, list[Pad]] = {}
    for p in pads:
        by_kind.setdefault(p.kind, []).append(p)

    gemma = gemma_physical_pad_order_enabled() and any(
        p.kind in {"dualsense", "exlene", "stream_ds5"} | NINTENDO_LAYOUT_KINDS for p in pads
    )
    if gemma:
        # Compact order from detect_pads: Exlene then DualSense (already sorted).
        physical = [p for p in pads if p.kind not in VIRTUAL_KINDS]
        out: list[Pad | None] = list(physical[:max_ports])
        while len(out) < max_ports:
            out.append(None)
        return out

    # Steam Link / Remote Play: always temp-override GCPad1 with the host
    # X360 pad when present (Exlene/ZhiXu on the Link box). Local fixed slots
    # shift down for that session only.
    remote_first = want_remote_xbox_p1()
    remote: Pad | None = None
    if remote_first:
        for k in ("steam_virtual", "moonlight_x360"):
            cands = by_kind.get(k) or []
            if cands:
                remote = cands.pop(0)
                break
        # Also honor explicit remote-play env even if kind tagging lagged.
        if remote is None and os.environ.get("BAZZITE_STEAM_REMOTE_PLAY", "").strip().lower() in {
            "1", "true", "yes",
        }:
            for k in ("steam_virtual", "moonlight_x360", "xbox"):
                cands = by_kind.get(k) or []
                if cands:
                    remote = cands.pop(0)
                    break

    out = []
    if remote is not None:
        out.append(remote)

    for kinds in DOLPHIN_FIXED_SLOTS:
        if len(out) >= max_ports:
            break
        chosen = None
        for k in kinds:
            cands = by_kind.get(k) or []
            if cands:
                chosen = cands.pop(0)
                break
        out.append(chosen)

    while len(out) < max_ports:
        out.append(None)
    return out[:max_ports]


def apply_dolphin(pads: list[Pad], max_ports: int = 4) -> list[str]:
    if not DOLPHIN_INI.is_file():
        raise SystemExit(f"Missing {DOLPHIN_INI}")

    original = DOLPHIN_INI.read_text(encoding="utf-8")
    existing = parse_gcpad_blocks(original)
    logs: list[str] = []
    new_blocks: list[str] = []
    slot_pads = _pads_for_dolphin_slots(pads, max_ports)
    remote_p1 = bool(
        slot_pads and slot_pads[0] is not None and slot_pads[0].kind in REMOTE_XBOX_KINDS
    )

    for port in range(1, max_ports + 1):
        pad = slot_pads[port - 1] if port - 1 < len(slot_pads) else None
        if pad is not None:
            block = gcpad_block(port, pad).rstrip("\n")
            labels = {
                "gamecube_nso": "NSO GameCube",
                "switch2_pro": "Switch 2 Pro",
                "joycon2": "Joy-Con 2",
                "joycon2_left": "Joy-Con 2 (L)",
                "joycon2_right": "Joy-Con 2 (R)",
                "exlene": "EXLENE",
                "dualsense": "DualSense",
                "stream_ds5": "Moonlight DS5",
                "steam_virtual": "Steam Link Xbox",
                "moonlight_x360": "Moonlight X360",
                "n64_nso": "NSO N64",
            }
            label = labels.get(pad.kind, pad.kind.upper())
            logs.append(
                f"GCPad{port} (Steam P{pad.steam_player or '?'}) <- {label} "
                f"[{pad.profile}] {pad.device}"
            )
        elif not pads and port in existing and "Device = " in existing[port]:
            device_line = next(
                (ln for ln in existing[port].splitlines() if ln.startswith("Device = ")),
                "Device = ",
            )
            if device_line.strip() != "Device =":
                block = existing[port]
                logs.append(f"GCPad{port} <- kept ({device_line.split('=', 1)[1].strip()})")
            else:
                block = gcpad_block(port, None).rstrip("\n")
                logs.append(f"GCPad{port} <- (empty — waiting for slot pad)")
        else:
            block = gcpad_block(port, None).rstrip("\n")
            if remote_p1 and port >= 2 and (port - 2) < len(DOLPHIN_FIXED_SLOTS):
                want = "/".join(DOLPHIN_FIXED_SLOTS[port - 2])
            else:
                want = (
                    "/".join(DOLPHIN_FIXED_SLOTS[port - 1])
                    if port - 1 < len(DOLPHIN_FIXED_SLOTS)
                    else "pad"
                )
            logs.append(f"GCPad{port} <- (empty — waiting for {want})")
        new_blocks.append(block)

    text = "\n".join(new_blocks) + "\n"
    if text != original:
        backup = DOLPHIN_INI.with_suffix(f".ini.bak.{datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(DOLPHIN_INI, backup)
        DOLPHIN_INI.write_text(text, encoding="utf-8")
        logs.append(f"Wrote {DOLPHIN_INI.name}")
    return logs


WIIMOTE_INI = DOLPHIN_INI.parent / "WiimoteNew.ini"


def apply_wiimote(pads: list[Pad], max_ports: int = 4) -> list[str]:
    """Point emulated Wiimote1-4 at the same real pads as GCPad1-4.

    Only the Device line inside each [WiimoteN] section is rewritten; button,
    IR, and motion bindings are preserved. Historically these were pinned to
    'SDL/x/Steam Deck Controller' (Steam virtual pads), which don't exist when
    Dolphin runs with Steam Input passthrough - leaving Wii games with no input.
    """
    if not WIIMOTE_INI.is_file():
        return [f"Wiimote: missing {WIIMOTE_INI}"]

    original = WIIMOTE_INI.read_text(encoding="utf-8")
    lines = original.splitlines()
    logs: list[str] = []
    current_port: int | None = None
    changed = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            if section.startswith("Wiimote") and section[7:].isdigit():
                current_port = int(section[7:])
            else:
                current_port = None
            continue
        if current_port is None or not (1 <= current_port <= max_ports):
            continue
        if stripped.startswith("Device ="):
            # Fixed Dolphin slots: same pad as GCPad N (GC / EXLENE / Pro2 / DS5).
            slot_pads = _pads_for_dolphin_slots(pads, max_ports)
            pad = slot_pads[current_port - 1] if current_port - 1 < len(slot_pads) else None
            if pad is not None and lines[i] != f"Device = {pad.device}":
                lines[i] = f"Device = {pad.device}"
                logs.append(f"Wiimote{current_port} <- {pad.name} ({pad.device})")
                changed = True
            elif pad is None and lines[i] != "Device = ":
                # Clear stale device when slot pad is offline
                lines[i] = "Device = "
                logs.append(f"Wiimote{current_port} <- (empty)")
                changed = True

    if changed:
        backup = WIIMOTE_INI.with_suffix(f".ini.bak.{datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(WIIMOTE_INI, backup)
        WIIMOTE_INI.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logs.append(f"Wrote {WIIMOTE_INI.name}")
    elif not logs:
        logs.append(
            "Wiimote: already in sync" if pads else "Wiimote: no pads (devices kept)"
        )
    return logs


def write_retroarch_overlay(pads: list[Pad], out: Path, max_players: int = 4) -> list[str]:
    # Always sdl2: SDL enumeration indices are captured exactly (sdl_idx), so
    # player order is deterministic. The udev driver's device order depends on
    # syspath sorting and breaks index-based player mapping. N64 rumble works
    # through SDL HIDAPI (launcher exports SDL_JOYSTICK_HIDAPI_SWITCH=1).
    driver = "sdl2"
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
            pad = pads[i]
            # Index must be in the DRIVER's enumeration space, not our sorted
            # order - otherwise P1/P2 swap whenever connect order differs.
            if driver == "sdl2":
                idx = pad.sdl_idx if pad.sdl_idx is not None else pad.evdev_idx
            else:
                idx = pad.udev_idx if pad.udev_idx is not None else pad.evdev_idx
            lines.append(f'input_player{player}_joypad_index = "{idx}"')
            logs.append(f"RetroArch player{player} <- {pad.name} ({driver} index {idx})")
        else:
            lines.append(f'input_player{player}_joypad_index = "-1"')
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logs.append(f"Wrote {out}")
    return logs




# --------------------------------------------------------------------------- #
# Eden (Switch) multi-player sync
# --------------------------------------------------------------------------- #
EDEN_TARGETS = (
    Path.home() / ".config/eden/qt-config.ini",
    Path.home() / ".config/EmuDeck/backend/configs/eden/config/qt-config.ini",
)
EDEN_INPUT_DIR = Path.home() / ".config/eden/input"
EDEN_SLOT_CACHE = Path.home() / ".config/bazzite/controller-sync/eden-fixed-slots.json"
EDEN_PROFILE_BY_KIND = {
    "gamecube_nso": "gc",
    "switch2_pro": "switch 2 pro",
    "joycon2": "joy-con 2",
    "joycon2_left": "joy-con 2 left",
    "joycon2_right": "joy-con 2 right",
    "exlene": "exlene",
    "dualsense": "DS5",
    "stream_ds5": "DS5",
    "mcon": "mcon",
    "xbox": "mcon",
    "steam_virtual": "mcon",
    "moonlight_x360": "mcon",
    "n64_nso": "n64",
    "generic": "switch 2 pro",
}
# Xbox-layout pads need face-button swap for Switch (A=East, B=South).
# steam_virtual (Steam Link X360) intentionally omitted: Exlene/ZhiXu through
# the Link already maps to correct Switch positions; swapping inverted A/B+X/Y
# on Remote Play only (local Exlene binding is unchanged).
EDEN_FACE_SWAP_KINDS = frozenset({
    "dualsense", "stream_ds5", "mcon", "xbox", "moonlight_x360",
})
DSU_GUID = "0000000000000000000000007f000001"
DSU_PORT = 26760


def _eden_guid_for_eden(guid: str) -> str:
    """Eden's SDL driver clears the name-CRC in GUID bytes 2-3 (see
    SDLDriver::GetGUID). System SDL includes the CRC — without zeroing those
    bytes, Eden never matches the device, the UI stays on 'Any', and input dies.
    """
    if len(guid) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", guid):
        return guid[:4] + "0000" + guid[8:]
    return guid


def _eden_sdl_prefix(guid: str, port: int) -> str:
    # Match Eden's BuildButtonParamPackageForButton: engine, port, guid.
    # Critically: Eden's `port` is the Nth joystick with this GUID (almost
    # always 0), NOT the global SDL joystick index. Writing sdl_idx as port
    # creates dummy null joysticks → dead input + UI stuck on "Any".
    g = _eden_guid_for_eden(guid)
    return f"engine:sdl,port:{port},guid:{g}"


def _eden_build_bindings_from_sdl(pad: "Pad", eden_port: int) -> dict[str, str]:
    """Build Eden Switch bindings from the live SDL GameController map.

    `eden_port` must be the per-GUID index Eden uses (0 for unique pads).
    """
    guid = pad.sdl_guid or ""
    if not guid or pad.sdl_idx is None:
        return {}
    try:
        import sdl2
        from sdl2 import gamecontroller as gc
    except ImportError:
        return {}

    if sdl2.SDL_WasInit(sdl2.SDL_INIT_GAMECONTROLLER) == 0:
        sdl2.SDL_Init(sdl2.SDL_INIT_GAMECONTROLLER | sdl2.SDL_INIT_JOYSTICK)

    idx = pad.sdl_idx
    if not sdl2.SDL_IsGameController(idx):
        return {}
    ctrl = sdl2.SDL_GameControllerOpen(idx)
    if not ctrl:
        return {}

    def btn(code: int) -> str | None:
        bind = sdl2.SDL_GameControllerGetBindForButton(ctrl, code)
        if bind.bindType == 1:  # button
            return f"{_eden_sdl_prefix(guid, eden_port)},button:{bind.value.button}"
        if bind.bindType == 3:  # hat
            # SDL hat mask → Eden direction name
            mask = int(bind.value.hat.hat_mask)
            direction = {
                1: "up", 2: "right", 4: "down", 8: "left",
            }.get(mask)
            if direction is None:
                return None
            return f"{_eden_sdl_prefix(guid, eden_port)},hat:{bind.value.hat.hat},direction:{direction}"
        return None

    def axis(code: int) -> str | None:
        bind = sdl2.SDL_GameControllerGetBindForAxis(ctrl, code)
        if bind.bindType == 2:
            return f"{_eden_sdl_prefix(guid, eden_port)},axis:{bind.value.axis}"
        return None

    # Raw SDL semantic → bind string
    raw = {
        "a": btn(gc.SDL_CONTROLLER_BUTTON_A),
        "b": btn(gc.SDL_CONTROLLER_BUTTON_B),
        "x": btn(gc.SDL_CONTROLLER_BUTTON_X),
        "y": btn(gc.SDL_CONTROLLER_BUTTON_Y),
        "back": btn(gc.SDL_CONTROLLER_BUTTON_BACK),
        "guide": btn(gc.SDL_CONTROLLER_BUTTON_GUIDE),
        "start": btn(gc.SDL_CONTROLLER_BUTTON_START),
        "leftstick": btn(gc.SDL_CONTROLLER_BUTTON_LEFTSTICK),
        "rightstick": btn(gc.SDL_CONTROLLER_BUTTON_RIGHTSTICK),
        "leftshoulder": btn(gc.SDL_CONTROLLER_BUTTON_LEFTSHOULDER),
        "rightshoulder": btn(gc.SDL_CONTROLLER_BUTTON_RIGHTSHOULDER),
        "dpup": btn(gc.SDL_CONTROLLER_BUTTON_DPAD_UP),
        "dpdown": btn(gc.SDL_CONTROLLER_BUTTON_DPAD_DOWN),
        "dpleft": btn(gc.SDL_CONTROLLER_BUTTON_DPAD_LEFT),
        "dpright": btn(gc.SDL_CONTROLLER_BUTTON_DPAD_RIGHT),
        "leftx": axis(gc.SDL_CONTROLLER_AXIS_LEFTX),
        "lefty": axis(gc.SDL_CONTROLLER_AXIS_LEFTY),
        "rightx": axis(gc.SDL_CONTROLLER_AXIS_RIGHTX),
        "righty": axis(gc.SDL_CONTROLLER_AXIS_RIGHTY),
        "lefttrigger": axis(gc.SDL_CONTROLLER_AXIS_TRIGGERLEFT),
        "righttrigger": axis(gc.SDL_CONTROLLER_AXIS_TRIGGERRIGHT),
    }
    sdl2.SDL_GameControllerClose(ctrl)

    # Face buttons: Xbox-layout pads need Switch diamond remap; ngc Nintendo pads
    # (Pro 2 / Joy-Con 2 / NSO GC) already expose A=SOUTH, B=EAST, X=WEST, Y=NORTH.
    if pad.kind in EDEN_FACE_SWAP_KINDS:
        face_a, face_b, face_x, face_y = raw["b"], raw["a"], raw["y"], raw["x"]
    elif pad.kind in NINTENDO_LAYOUT_KINDS:
        face_a, face_b, face_x, face_y = raw["a"], raw["b"], raw["x"], raw["y"]
    else:
        face_a, face_b, face_x, face_y = raw["a"], raw["b"], raw["x"], raw["y"]

    def q(s: str | None) -> str:
        return f'"{s}"' if s else "[empty]"

    def stick(ax: str | None, ay: str | None) -> str:
        if not ax or not ay:
            return "[empty]"
        # axis strings are full engine:sdl,...axis:N — extract axis numbers
        mx = re.search(r"axis:(\d+)", ax)
        my = re.search(r"axis:(\d+)", ay)
        if not mx or not my:
            return "[empty]"
        return (
            f'"{_eden_sdl_prefix(guid, eden_port)},axis_x:{mx.group(1)},axis_y:{my.group(1)},'
            f'offset_x:0,offset_y:0,invert_x:+,invert_y:+"'
        )

    def trigger(ax: str | None) -> str:
        if not ax:
            return "[empty]"
        m = re.search(r"axis:(\d+)", ax)
        if not m:
            return "[empty]"
        return f'"{_eden_sdl_prefix(guid, eden_port)},axis:{m.group(1)},threshold:0.5,invert:+"'

    bindings = {
        "button_a": q(face_a),
        "button_b": q(face_b),
        "button_x": q(face_x),
        "button_y": q(face_y),
        "button_lstick": q(raw["leftstick"]),
        "button_rstick": q(raw["rightstick"]),
        "button_l": q(raw["leftshoulder"]),
        "button_r": q(raw["rightshoulder"]),
        "button_zl": trigger(raw["lefttrigger"]),
        "button_zr": trigger(raw["righttrigger"]),
        "button_plus": q(raw["start"]),
        "button_minus": q(raw["back"]),
        "button_dleft": q(raw["dpleft"]),
        "button_dup": q(raw["dpup"]),
        "button_dright": q(raw["dpright"]),
        "button_ddown": q(raw["dpdown"]),
        "button_slleft": q(raw["leftshoulder"]),
        "button_srleft": q(raw["rightshoulder"]),
        "button_slright": q(raw["leftshoulder"]),
        "button_srright": q(raw["rightshoulder"]),
        "button_home": q(raw["guide"]),
        "button_screenshot": "[empty]",
        "lstick": stick(raw["leftx"], raw["lefty"]),
        "rstick": stick(raw["rightx"], raw["righty"]),
    }
    return bindings


def _eden_write_profile(profile: str, bindings: dict[str, str], motion: str) -> None:
    """Keep on-disk profiles in sync — Eden reloads them by profile_name."""
    EDEN_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = EDEN_INPUT_DIR / f"{profile}.ini"
    lines = ["[Controls]", "type\\default=true", "type=0"]
    for key, value in bindings.items():
        lines.append(f"{key}\\default=false")
        lines.append(f"{key}={value}")
    for side in ("motionleft", "motionright"):
        lines.append(f"{side}\\default=false")
        lines.append(f"{side}={motion}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _eden_motion(pad: "Pad", guid: str, eden_port: int) -> str:
    m = re.search(r"\(P(\d)\)\s*$", pad.name)
    if m:
        dsu_pad = max(0, int(m.group(1)) - 1)
        return (
            f'"engine:cemuhookudp,guid:{DSU_GUID},port:{DSU_PORT},'
            f'pad:{dsu_pad},motion:0"'
        )
    if guid and pad.kind in ("dualsense", "stream_ds5", "switch2_pro", "joycon2", "joycon2_left", "joycon2_right", "exlene"):
        g = _eden_guid_for_eden(guid)
        return f'"engine:sdl,port:{eden_port},guid:{g},motion:0"'
    return "[empty]"


def _eden_player_block(player: int, pad: "Pad", eden_port: int) -> list[str]:
    # Unique profile per player slot so two Switch Pros do not clobber one file.
    kind_label = EDEN_PROFILE_BY_KIND.get(pad.kind, "pad")
    profile = f"P{player + 1}-{kind_label}"
    guid = pad.sdl_guid or ""
    bindings = _eden_build_bindings_from_sdl(pad, eden_port)
    motion = _eden_motion(pad, guid, eden_port)
    if bindings:
        _eden_write_profile(profile, bindings, motion)

    lines = [
        f"player_{player}_connected=true",
        f"player_{player}_connected\\default=false",
        f"player_{player}_type=0",
        f"player_{player}_type\\default=false",
        f"player_{player}_profile_name={profile}",
        f"player_{player}_profile_name\\default=false",
        f"player_{player}_vibration_enabled=true",
        f"player_{player}_vibration_enabled\\default=false",
    ]
    for key, value in bindings.items():
        lines.append(f"player_{player}_{key}={value}")
        lines.append(f"player_{player}_{key}\\default=false")
    for side in ("motionleft", "motionright"):
        lines.append(f"player_{player}_{side}={motion}")
        lines.append(f"player_{player}_{side}\\default=false")
    return lines


# Eden / Switch fixed slots (do NOT compact — keeps DS5 on P3 and N64 on P4).
# P1 Switch 2 Pro, P2 NSO GameCube, P3 DualSense, P4 NSO N64.
EDEN_FIXED_SLOTS = (
    ("switch2_pro", "joycon2", "joycon2_left", "joycon2_right"),
    ("gamecube_nso",),
    ("dualsense", "stream_ds5"),
    ("n64_nso",),
)

# Used only for LED priority / docs when listing.
EDEN_KIND_PRIORITY = {
    "steam_virtual": -2,
    "moonlight_x360": -1,
    "switch2_pro": 0,
    "joycon2": 0,
    "joycon2_left": 0,
    "joycon2_right": 0,
    "gamecube_nso": 1,
    "dualsense": 2,
    "stream_ds5": 2,
    "n64_nso": 3,
    "mcon": 4,
    "exlene": 5,
    "xbox": 6,
}

def _order_pads_for_eden(pads: list[Pad]) -> list[Optional[Pad]]:
    """Return length-4 list; missing kinds are None (slot stays empty).

    When a Steam Link / Remote Play Xbox pad is present, it takes P1 and the
    usual fixed kinds fill P2–P4. When it's gone, P1 reverts to Switch 2 Pro.

    Gemma + DualSense/Exlene: compact order (Exlene then DualSense) like Dolphin.
    """
    gemma = gemma_physical_pad_order_enabled() and any(
        p.kind in {"dualsense", "exlene", "stream_ds5"} | NINTENDO_LAYOUT_KINDS for p in pads
    )
    if gemma:
        physical = [
            p
            for p in pads
            if p.kind not in VIRTUAL_KINDS
            and (p.sdl_guid or p.path.startswith("/dev/") or p.path.startswith("sdl:"))
        ]
        out: list[Optional[Pad]] = list(physical[:4])
        while len(out) < 4:
            out.append(None)
        return out

    by_kind: dict[str, list[Pad]] = {}
    for p in pads:
        if not p.sdl_guid or p.sdl_idx is None:
            continue
        by_kind.setdefault(p.kind, []).append(p)

    remote_first = want_remote_xbox_p1()
    remote: Optional[Pad] = None
    if remote_first:
        for k in ("steam_virtual", "moonlight_x360"):
            cands = by_kind.get(k) or []
            if cands:
                remote = cands.pop(0)
                break

    out = []
    if remote is not None:
        out.append(remote)

    for kinds in EDEN_FIXED_SLOTS:
        if len(out) >= 4:
            break
        chosen = None
        for k in kinds:
            cands = by_kind.get(k) or []
            if cands:
                chosen = cands.pop(0)
                break
        out.append(chosen)

    while len(out) < 4:
        out.append(None)
    return out[:4]


def _eden_empty_player_block(player: int) -> list[str]:
    return [
        f"player_{player}_connected=false",
        f"player_{player}_connected\\default={'true' if player == 0 else 'false'}",
        f"player_{player}_profile_name=",
        f"player_{player}_profile_name\\default=true",
    ]


def _eden_load_slot_cache() -> dict:
    if not EDEN_SLOT_CACHE.is_file():
        return {}
    try:
        data = json.loads(EDEN_SLOT_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _eden_save_slot_cache(cache: dict) -> None:
    EDEN_SLOT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    EDEN_SLOT_CACHE.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")


def _eden_cache_pad(cache: dict, slot: int, pad: "Pad", bindings: dict[str, str], motion: str) -> None:
    """Remember live pad so Eden keeps the binding when it briefly disconnects."""
    kind_label = EDEN_PROFILE_BY_KIND.get(pad.kind, "pad")
    cache[str(slot)] = {
        "kind": pad.kind,
        "name": pad.name,
        "mac": pad.mac or "",
        "guid": _eden_guid_for_eden(pad.sdl_guid or ""),
        "profile": f"P{slot + 1}-{kind_label}",
        "bindings": bindings,
        "motion": motion,
    }


def _eden_player_block_from_cache(player: int, entry: dict) -> list[str]:
    profile = entry.get("profile") or f"P{player + 1}-pad"
    bindings = entry.get("bindings") or {}
    motion = entry.get("motion") or "[empty]"
    if bindings:
        _eden_write_profile(profile, bindings, motion)
    lines = [
        f"player_{player}_connected=true",
        f"player_{player}_connected\\default=false",
        f"player_{player}_type=0",
        f"player_{player}_type\\default=false",
        f"player_{player}_profile_name={profile}",
        f"player_{player}_profile_name\\default=false",
        f"player_{player}_vibration_enabled=true",
        f"player_{player}_vibration_enabled\\default=false",
    ]
    for key, value in bindings.items():
        lines.append(f"player_{player}_{key}={value}")
        lines.append(f"player_{player}_{key}\\default=false")
    for side in ("motionleft", "motionright"):
        lines.append(f"player_{player}_{side}={motion}")
        lines.append(f"player_{player}_{side}\\default=false")
    return lines


def apply_eden(pads: list[Pad], max_players: int = 4) -> list[str]:
    """Rebind Eden P1–P4: Remote Play Xbox as P1 when present, else fixed kinds."""
    gemma = gemma_physical_pad_order_enabled() and any(
        p.kind in {"dualsense", "exlene", "stream_ds5"} | NINTENDO_LAYOUT_KINDS for p in pads
    )
    if gemma:
        candidates = list(pads)
    else:
        candidates = [p for p in pads if p.sdl_guid and p.sdl_idx is not None]
    slots = _order_pads_for_eden(candidates)
    cache = _eden_load_slot_cache()
    logs: list[str] = []
    guid_seen: dict[str, int] = {}
    blocks: list[str] = []
    remote_p1 = bool(slots and slots[0] is not None and slots[0].kind in REMOTE_XBOX_KINDS)
    for player in range(max(max_players, 4)):
        pad = slots[player] if player < len(slots) else None
        if pad is not None:
            g = pad.sdl_guid or ""
            eden_port = guid_seen.get(g, 0)
            guid_seen[g] = eden_port + 1
            block = _eden_player_block(player, pad, eden_port)
            bindings = _eden_build_bindings_from_sdl(pad, eden_port)
            motion = _eden_motion(pad, g, eden_port)
            # Only cache fixed-kind pads into their home slots so a temporary
            # Remote Play Xbox on P1 does not wipe the Switch 2 Pro binding.
            if bindings and pad.kind not in REMOTE_XBOX_KINDS:
                home = next(
                    (
                        i
                        for i, kinds in enumerate(EDEN_FIXED_SLOTS)
                        if pad.kind in kinds
                    ),
                    None,
                )
                if home is not None:
                    _eden_cache_pad(cache, home, pad, bindings, motion)
            blocks.extend(block)
            a_line = next((ln for ln in block if ln.startswith(f"player_{player}_button_a=")), "")
            prof = next(
                (ln.split("=", 1)[1] for ln in block if ln.startswith(f"player_{player}_profile_name=")
                 and "\\default" not in ln),
                "?",
            )
            logs.append(
                f"Eden P{player + 1} <- {pad.name} [{prof}] "
                f"eden_port={eden_port} sdl_idx={pad.sdl_idx} "
                f"{a_line.split('=',1)[-1][:70]}"
            )
            continue

        # Slot empty live — keep last known DualSense/N64/etc so Eden doesn't show Any.
        # When Remote Play owns P1, fixed-kind caches map to shifted slots (P2+).
        # Gemma DualSense/Exlene compact mode: do not revive offline Trevor pads.
        if gemma:
            blocks.extend(_eden_empty_player_block(player))
            logs.append(f"Eden P{player + 1} <- (empty)")
            continue
        cached = None
        expected_kinds: tuple[str, ...] = ()
        if remote_p1:
            # P1 empty without a live remote pad shouldn't happen here; P2+ map
            # to fixed slots 0..2
            if player >= 1 and (player - 1) < len(EDEN_FIXED_SLOTS):
                expected_kinds = EDEN_FIXED_SLOTS[player - 1]
                cached = cache.get(str(player - 1))
        else:
            expected_kinds = EDEN_FIXED_SLOTS[player] if player < len(EDEN_FIXED_SLOTS) else ()
            cached = cache.get(str(player))
        if cached and cached.get("kind") in expected_kinds and cached.get("bindings"):
            blocks.extend(_eden_player_block_from_cache(player, cached))
            logs.append(
                f"Eden P{player + 1} <- cached {cached.get('name')} "
                f"[{cached.get('profile')}] (offline — kept binding)"
            )
        else:
            blocks.extend(_eden_empty_player_block(player))
            want = "/".join(expected_kinds) if expected_kinds else "pad"
            logs.append(f"Eden P{player + 1} <- (empty — waiting for {want})")

    for player in range(max(max_players, 4), 8):
        blocks.extend(_eden_empty_player_block(player))

    _eden_save_slot_cache(cache)

    globals_force = {
        "disableControllerApplet": "true",
        "controller_applet_mode": "0",
        "motion_enabled": "true",
        "vibration_enabled": "true",
        "udp_input_servers": f"127.0.0.1:{DSU_PORT}",
    }

    for target in EDEN_TARGETS:
        if not target.is_file():
            continue
        original = target.read_text(encoding="utf-8", errors="replace")
        out: list[str] = []
        for line in original.splitlines():
            if re.match(r"^player_\d+_", line):
                continue
            key = line.split("=", 1)[0]
            base = key.split("\\", 1)[0]
            if base in globals_force and "=" in line:
                if key.endswith("\\default"):
                    out.append(f"{key}=false")
                else:
                    out.append(f"{key}={globals_force[base]}")
                continue
            out.append(line)

        insert = len(out)
        for i, line in enumerate(out):
            if line == "pause_tas_on_load\\default=true":
                insert = i + 1
                break
            if line == "[Controls]":
                insert = i + 1
        out[insert:insert] = blocks
        text = "\n".join(out) + "\n"
        if text != original:
            shutil.copy2(
                target, target.with_suffix(target.suffix + f".bak.{datetime.now():%Y%m%d%H%M%S}")
            )
            target.write_text(text, encoding="utf-8")
            logs.append(f"Wrote {target}")
    return logs



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="Print detected pads as JSON")
    ap.add_argument("--dolphin", action="store_true", help="Apply GCPad1-4 in Dolphin")
    ap.add_argument("--retroarch", metavar="OUT", help="Write RetroArch appendconfig")
    ap.add_argument("--eden", action="store_true", help="Rebind Eden players 1-4")
    ap.add_argument(
        "--leds",
        nargs="?",
        const="auto",
        default=None,
        metavar="MODE",
        help="Set controller player LEDs (auto|eden|dolphin). "
        "Also runs after --eden/--dolphin when an emulator matching that mode is active.",
    )
    ap.add_argument("--max-players", type=int, default=4)
    args = ap.parse_args()

    pads = detect_pads()
    if args.json:
        print(json.dumps([asdict(p) for p in pads], indent=2))
        return 0

    if not args.dolphin and not args.retroarch and not args.eden and not args.leds:
        args.dolphin = True

    if args.dolphin:
        for line in apply_dolphin(pads, args.max_players):
            print(line)
        # WiimoteNew.ini is left alone by default so Wii titles keep their
        # emulated-Wiimote / pointer setup. Opt in with BAZZITE_SYNC_WIIMOTE=1.
        if os.environ.get("BAZZITE_SYNC_WIIMOTE", "").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            for line in apply_wiimote(pads, args.max_players):
                print(line)
        else:
            print("Wiimote: preserved (untouched)")

    if args.retroarch:
        for line in write_retroarch_overlay(pads, Path(args.retroarch), args.max_players):
            print(line)

    if args.eden:
        for line in apply_eden(pads, args.max_players):
            print(line)

    # Player LEDs: only when --leds is explicitly passed.
    # Auto-inferring from running emulators raced Eden vs Dolphin and flipped
    # Pro P1<->P3 every few seconds (breaks pad names + input).
    led_mode = args.leds
    if led_mode:
        led_script = Path.home() / ".local/bin/bazzite-set-player-leds.py"
        if led_script.is_file():
            cmd = ["python3", str(led_script), "--max-players", str(args.max_players)]
            if led_mode != "auto":
                cmd.extend(["--mode", led_mode])
            try:
                out = subprocess.check_output(cmd, text=True, timeout=12)
                print(out.rstrip())
            except Exception as exc:
                print(f"LEDs: failed ({exc})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
