#!/usr/bin/env python3
"""Set physical player-indicator LEDs to match emulator player order.

Eden (Switch):  P1 Switch 2 Pro, P2 GC, P3 DualSense, P4 EXLENE…
Dolphin/global: P1 GC, P2 Switch 2 Pro, P3 DualSense, P4 EXLENE…

Bridge pads (NSO GC / Pro 2) are updated via ~/.config/nso-gc/led-players.json
(read by nso-gc). DualSense / EXLENE / N64 use kernel sysfs player LEDs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DETECTOR = Path.home() / ".local/bin/bazzite-controller-detect.py"
LED_STATE = Path.home() / ".config/bazzite/controller-sync/led-mode.txt"
BRIDGE_LED_FILE = Path.home() / ".config/nso-gc/led-players.json"
LEDS_ROOT = Path("/sys/class/leds")

# DualSense lightbar colors (optional flair) — R G B
LIGHTBAR = {
    1: (0, 60, 255),    # blue
    2: (255, 40, 40),   # red
    3: (40, 220, 60),   # green
    4: (255, 60, 200),  # pink
    5: (255, 180, 0),   # amber
}

BRIDGE_KINDS = frozenset({
    "gamecube_nso", "switch2_pro", "joycon2", "joycon2_left", "joycon2_right",
})


def _ps_args() -> str:
    try:
        return subprocess.check_output(["ps", "-eo", "args"], text=True, timeout=2)
    except Exception:
        return ""


def detect_active_mode() -> str:
    """Pick LED order from whatever emulator is actually running."""
    ps = _ps_args()
    if re.search(r"/bin/eden\b|Eden\.AppImage|eden-game\.sh", ps):
        return "eden"
    if re.search(r"[Dd]olphin[- ]?[Ee]mu|dolphin-game\.sh|dolphin-emu", ps):
        return "dolphin"
    if re.search(r"[Rr]yujinx|ryujinx-game\.sh", ps):
        return "eden"  # Switch-style: Pro 2 as P1
    if LED_STATE.is_file():
        mode = LED_STATE.read_text(encoding="utf-8").strip().lower()
        if mode in {"eden", "dolphin"}:
            return mode
    return "dolphin"


def remember_mode(mode: str) -> None:
    LED_STATE.parent.mkdir(parents=True, exist_ok=True)
    LED_STATE.write_text(mode + "\n", encoding="utf-8")


def detect_pads() -> list[dict]:
    if not DETECTOR.is_file():
        return []
    out = subprocess.check_output(
        ["python3", str(DETECTOR), "--json"], text=True, timeout=10
    )
    pads = json.loads(out)
    return pads if isinstance(pads, list) else []


def order_pads(pads: list[dict], mode: str) -> list[dict | None]:
    """Match bazzite-controller-detect.py Eden vs Dolphin priorities.

    Both Eden and Dolphin use fixed slots (None = empty).
    """
    if mode == "eden":
        slots = (
            ("switch2_pro", "joycon2", "joycon2_left", "joycon2_right"),
            ("gamecube_nso",),
            ("dualsense", "stream_ds5"),
            ("n64_nso",),
        )
    else:
        slots = (
            ("gamecube_nso",),
            ("exlene",),
            ("switch2_pro", "joycon2", "joycon2_left", "joycon2_right"),
            ("dualsense", "stream_ds5"),
        )
    by_kind: dict[str, list[dict]] = {}
    for p in pads:
        if p.get("kind") in {"steam_virtual", "moonlight_x360"}:
            continue
        by_kind.setdefault(str(p.get("kind") or ""), []).append(p)
    out: list[dict | None] = []
    for kinds in slots:
        chosen = None
        for k in kinds:
            cands = by_kind.get(k) or []
            if cands:
                chosen = cands.pop(0)
                break
        out.append(chosen)
    return out


def _write_brightness(path: Path, value: int) -> bool:
    try:
        path.write_text(str(int(value)), encoding="utf-8")
        return True
    except PermissionError:
        try:
            subprocess.run(
                ["sudo", "-n", "/usr/local/bin/bazzite-fix-controller-led-perms"],
                timeout=2,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            path.write_text(str(int(value)), encoding="utf-8")
            return True
        except Exception:
            pass
        try:
            subprocess.run(
                ["sudo", "-n", "tee", str(path)],
                input=str(int(value)).encode(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=True,
            )
            return True
        except Exception:
            return False
    except Exception:
        return False


def ensure_led_perms() -> None:
    helper = Path("/usr/local/bin/bazzite-fix-controller-led-perms")
    if helper.is_file():
        subprocess.run(
            ["sudo", "-n", str(helper)],
            timeout=2,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _input_names_for_mac(mac: str) -> list[str]:
    mac_u = mac.upper()
    names: list[str] = []
    for p in Path("/sys/class/input").glob("input*"):
        uf = p / "uniq"
        if not uf.is_file():
            continue
        try:
            if uf.read_text().strip().upper() != mac_u:
                continue
        except OSError:
            continue
        name = ""
        nf = p / "name"
        if nf.is_file():
            try:
                name = nf.read_text().strip().lower()
            except OSError:
                pass
        if any(x in name for x in ("motion", "touchpad", "consumer")):
            continue
        names.append(p.name)
    return names


def _hid_names_for_mac(mac: str) -> list[str]:
    mac_u = mac.upper()
    out: list[str] = []
    hid_root = Path("/sys/bus/hid/devices")
    if not hid_root.is_dir():
        return out
    for hid in hid_root.iterdir():
        got = ""
        uf = hid / "uniq"
        if uf.is_file():
            try:
                got = uf.read_text().strip().upper()
            except OSError:
                pass
        if not got:
            ue = hid / "uevent"
            if ue.is_file():
                try:
                    for line in ue.read_text().splitlines():
                        if line.startswith("HID_UNIQ="):
                            got = line.split("=", 1)[1].strip().upper()
                            break
                except OSError:
                    pass
        if got == mac_u:
            out.append(hid.name)
    return out


def _led_groups_for_mac(mac: str) -> list[tuple[str, list[Path]]]:
    """Return [(prefix, [player-1 path, player-2, ...]), ...] for a MAC."""
    if not mac or not LEDS_ROOT.is_dir():
        return []
    prefixes: set[str] = set()
    for inp in _input_names_for_mac(mac):
        for led in LEDS_ROOT.glob(f"{inp}:*:player-1"):
            prefixes.add(led.name[: -len(":player-1")])
    for hid in _hid_names_for_mac(mac):
        for led in LEDS_ROOT.glob(f"{hid}:*:player-1"):
            prefixes.add(led.name[: -len(":player-1")])

    groups: list[tuple[str, list[Path]]] = []
    for prefix in sorted(prefixes):
        players: list[Path] = []
        for i in range(1, 9):
            p = LEDS_ROOT / f"{prefix}:player-{i}"
            if (p / "brightness").is_file() or p.is_dir():
                # brightness is a file under the led dir
                bright = p / "brightness" if p.is_dir() else p
                if not bright.is_file() and p.is_file():
                    bright = p
                if (p / "brightness").is_file():
                    players.append(p / "brightness")
        if players:
            groups.append((prefix, players))
    return groups


def set_sysfs_player_leds(mac: str, player: int) -> list[str]:
    logs: list[str] = []
    groups = _led_groups_for_mac(mac)
    if not groups:
        logs.append(f"  sysfs: no player LEDs for {mac}")
        return logs
    for prefix, brightness_paths in groups:
        ok = 0
        for i, bright in enumerate(brightness_paths, start=1):
            want = 1 if i == player else 0
            if _write_brightness(bright, want):
                ok += 1
        logs.append(f"  sysfs {prefix}: player {player} ({ok}/{len(brightness_paths)} leds)")
        # DualSense lightbar
        bar = LEDS_ROOT / f"{prefix.split(':')[0]}:rgb:indicator"
        # prefix is like "input158:white" — rgb is sibling "input158:rgb:indicator"
        inp = prefix.split(":", 1)[0]
        rgb_dir = LEDS_ROOT / f"{inp}:rgb:indicator"
        multi = rgb_dir / "multi_intensity"
        bright = rgb_dir / "brightness"
        if multi.is_file():
            r, g, b = LIGHTBAR.get(player, (255, 255, 255))
            try:
                multi.write_text(f"{r} {g} {b}\n", encoding="utf-8")
                if bright.is_file():
                    bright.write_text("255\n", encoding="utf-8")
                logs.append(f"  lightbar {inp}: rgb({r},{g},{b})")
            except Exception:
                if _write_brightness(multi, 0):  # unlikely path
                    pass
                else:
                    try:
                        subprocess.run(
                            ["sudo", "-n", "tee", str(multi)],
                            input=f"{r} {g} {b}\n".encode(),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=2,
                            check=False,
                        )
                        logs.append(f"  lightbar {inp}: rgb({r},{g},{b}) via sudo")
                    except Exception:
                        logs.append(f"  lightbar {inp}: write failed")
    return logs


def write_bridge_led_map(mac_to_player: dict[str, int]) -> None:
    BRIDGE_LED_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {m.upper(): int(p) for m, p in mac_to_player.items()}
    BRIDGE_LED_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def apply_leds(mode: str | None = None, max_players: int = 4) -> list[str]:
    mode = (mode or detect_active_mode()).lower()
    if mode not in {"eden", "dolphin"}:
        mode = "dolphin"
    remember_mode(mode)
    ensure_led_perms()

    pads = detect_pads()
    ordered = order_pads(pads, mode)[:max_players]
    logs = [f"LED mode={mode} ({sum(1 for p in ordered if p)} pads bound)"]

    bridge_map: dict[str, int] = {}
    for i, pad in enumerate(ordered):
        player = i + 1
        if pad is None:
            logs.append(f"P{player}: (empty)")
            continue
        mac = (pad.get("mac") or "").upper()
        kind = pad.get("kind") or "?"
        name = pad.get("name") or kind
        logs.append(f"P{player}: {name} [{kind}] {mac or '-'}")

        if kind in BRIDGE_KINDS and mac:
            bridge_map[mac] = player
        elif mac:
            logs.extend(set_sysfs_player_leds(mac, player))
        else:
            logs.append("  (no MAC — skipped sysfs)")

    write_bridge_led_map(bridge_map)
    logs.append(f"Wrote bridge LED map ({len(bridge_map)}): {BRIDGE_LED_FILE}")
    return logs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--mode",
        choices=("auto", "eden", "dolphin"),
        default="auto",
        help="Player order for LEDs (default: auto from running emulator)",
    )
    ap.add_argument("--max-players", type=int, default=4)
    args = ap.parse_args()
    mode = None if args.mode == "auto" else args.mode
    for line in apply_leds(mode, args.max_players):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
