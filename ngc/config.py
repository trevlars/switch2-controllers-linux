"""Persistent configuration for the bridge.

Supports multiple bonded controllers (for local multiplayer): each entry has a
MAC, an assigned player slot, and a remembered name. Stored as JSON under XDG
config. Legacy single-controller configs are migrated automatically on load.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "nso-gc"
CONFIG_PATH = CONFIG_DIR / "config.json"
CONFIG_BACKUP = CONFIG_DIR / "config.json.bak"


def _adapter_address_from_bluez(hci: str) -> Optional[str]:
    """Ask BlueZ for an adapter's address over D-Bus (no root needed)."""
    try:
        out = subprocess.run(
            ["busctl", "get-property", "org.bluez", f"/org/bluez/{hci}",
             "org.bluez.Adapter1", "Address"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    parts = out.stdout.strip().split('"')
    return parts[1].upper() if len(parts) >= 2 else None


def detect_adapter() -> Optional[str]:
    """Return the first Bluetooth adapter's address (no root needed).

    Older kernels expose /sys/class/bluetooth/hciN/address. Some newer ones do
    not, so fall back to asking BlueZ directly.
    """
    base = Path("/sys/class/bluetooth")
    if not base.exists():
        return None
    names = [hci.name for hci in sorted(base.iterdir()) if ":" not in hci.name]
    for name in names:
        addr = base / name / "address"
        if addr.exists():
            try:
                return addr.read_text().strip().upper()
            except OSError:
                continue
    for name in names:
        addr_str = _adapter_address_from_bluez(name)
        if addr_str:
            return addr_str
    return None


PRO_CONTROLLER2_PID = 0x2069
GRIP_BUTTONS = ("GL", "GR")
ASSIGNABLE_BUTTONS = (
    "A", "B", "X", "Y", "L", "R", "ZL", "ZR", "PLUS", "MINUS", "HOME", "CAPTURE",
    "C", "L_STK", "R_STK", "UP", "DOWN", "LEFT", "RIGHT",
)


@dataclass
class ControllerEntry:
    mac: str
    player: int = 1
    name: str = ""
    bonded: bool = False
    grip_map: dict = field(default_factory=dict)


@dataclass
class Config:
    controllers: list = field(default_factory=list)  # list of {mac, player, name}
    adapter_mac: Optional[str] = None
    button_map: dict = field(default_factory=dict)
    # Rumble: GameCube uses safe presets; Pro/Joy-Con use the real HD motor.
    enable_rumble: bool = True
    # Magnitude (0..1) at which a GameCube pad switches from soft to strong rumble.
    gc_impact_threshold: float = 0.52
    # Disconnect and sleep the pad after this many seconds with no button presses.
    # Set to 0 to disable. Override with NGC_IDLE_SLEEP_S / NGC_IDLE_SLEEP=0.
    idle_sleep_s: float = 300.0
    # Legacy single-controller fields (migrated into `controllers` on load).
    controller_mac: Optional[str] = None
    player: int = 1

    @classmethod
    def _read_json(cls, path: Path) -> dict:
        return json.loads(path.read_text())

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            try:
                data = cls._read_json(CONFIG_PATH)
            except Exception:
                if CONFIG_BACKUP.exists():
                    try:
                        data = cls._read_json(CONFIG_BACKUP)
                    except Exception:
                        cfg = cls()
                    else:
                        cfg = cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})
                else:
                    cfg = cls()
            else:
                cfg = cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})
        else:
            cfg = cls()
        cfg._migrate()
        if not cfg.adapter_mac:
            cfg.adapter_mac = detect_adapter()
        _apply_env_overrides(cfg)
        return cfg

    def _migrate(self) -> None:
        """Fold a legacy single controller into the controllers list."""
        if not self.controllers and self.controller_mac:
            self.controllers = [
                {"mac": self.controller_mac, "player": self.player, "name": ""}
            ]
            self.controller_mac = None

    def entries(self) -> list[ControllerEntry]:
        return [
            ControllerEntry(
                mac=c["mac"],
                player=c.get("player", i + 1),
                name=c.get("name", ""),
                bonded=bool(c.get("bonded", False)),
                grip_map=self.grip_map_for(c["mac"]),
            )
            for i, c in enumerate(self.controllers)
        ]

    def set_product(self, mac: str, product_id: int) -> bool:
        """Remember a controller's USB product ID; True if it changed."""
        for c in self.controllers:
            if c["mac"].upper() == mac.upper() and c.get("product_id") != product_id:
                c["product_id"] = product_id
                return True
        return False

    def supports_grips(self, mac: str) -> bool:
        """Only the Pro Controller 2 has GL/GR (unknown models are allowed until seen)."""
        for c in self.controllers:
            if c["mac"].upper() == mac.upper():
                pid = c.get("product_id")
                return pid is None or pid == PRO_CONTROLLER2_PID
        return False

    def grip_map_for(self, mac: str) -> dict:
        """Per-controller GL/GR assignment, e.g. {"GL": "A", "GR": "ZL"}."""
        if not self.supports_grips(mac):
            return {}
        for c in self.controllers:
            if c["mac"].upper() == mac.upper():
                gm = c.get("grip_map") or {}
                return {k: v for k, v in gm.items() if k in GRIP_BUTTONS and v in ASSIGNABLE_BUTTONS}
        return {}

    def set_grip(self, mac: str, grip: str, target: Optional[str]) -> bool:
        """Assign (or clear with None) a button for GL/GR on one controller."""
        grip = grip.upper()
        if grip not in GRIP_BUTTONS:
            raise ValueError(f"grip must be one of {GRIP_BUTTONS}")
        if target is not None:
            target = target.upper()
            if target not in ASSIGNABLE_BUTTONS:
                raise ValueError(f"unknown button {target!r}")
        if not self.supports_grips(mac):
            raise ValueError("only the Pro Controller 2 has GL/GR buttons")
        for c in self.controllers:
            if c["mac"].upper() == mac.upper():
                gm = dict(c.get("grip_map") or {})
                if target is None:
                    gm.pop(grip, None)
                else:
                    gm[grip] = target
                c["grip_map"] = gm
                return True
        return False

    def set_grips(self, mac: str, grips: dict) -> None:
        for c in self.controllers:
            if c["mac"].upper() == mac.upper():
                c["grip_map"] = dict(grips)
                return

    def mark_bonded(self, mac: str, bonded: bool = True) -> None:
        for c in self.controllers:
            if c["mac"].upper() == mac.upper():
                c["bonded"] = bonded
                return

    def is_bonded(self, mac: str) -> bool:
        for c in self.controllers:
            if c["mac"].upper() == mac.upper():
                return bool(c.get("bonded", False))
        return False

    def add_controller(self, mac: str, name: str = "", player: int | None = None) -> ControllerEntry:
        """Add (or update) a controller, assigning the next free player slot."""
        mac = mac.upper()
        for c in self.controllers:
            if c["mac"].upper() == mac:
                if name:
                    c["name"] = name
                if player is not None:
                    used = {x.get("player") for x in self.controllers if x["mac"].upper() != mac}
                    if player in used:
                        raise ValueError(f"player {player} already in use")
                    c["player"] = player
                return ControllerEntry(
                    c["mac"], c.get("player", 1), c.get("name", ""), bool(c.get("bonded", False))
                )
        if player is not None:
            if any(c.get("player") == player for c in self.controllers):
                raise ValueError(f"player {player} already in use")
            assigned = player
        else:
            used = {c.get("player", 0) for c in self.controllers}
            assigned = next((p for p in range(1, 9) if p not in used), None)
            if assigned is None:
                raise ValueError("maximum 8 controllers already saved")
        entry = {"mac": mac, "player": assigned, "name": name, "bonded": False}
        self.controllers.append(entry)
        return ControllerEntry(mac, assigned, name, False)

    def remove_controller(self, mac: str) -> bool:
        mac = mac.upper()
        before = len(self.controllers)
        self.controllers = [c for c in self.controllers if c["mac"].upper() != mac]
        return len(self.controllers) < before

    def swap_players(self, player_a: int, player_b: int) -> bool:
        ca = cb = None
        for c in self.controllers:
            if c.get("player") == player_a:
                ca = c
            elif c.get("player") == player_b:
                cb = c
        if not ca or not cb:
            return False
        ca["player"], cb["player"] = player_b, player_a
        return True

    def find_by_player(self, player: int) -> Optional[ControllerEntry]:
        for c in self.controllers:
            if c.get("player") == player:
                return ControllerEntry(
                    c["mac"], c.get("player", 1), c.get("name", ""), bool(c.get("bonded", False))
                )
        return None

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Drop legacy fields from the serialized form once migrated.
        data = asdict(self)
        if self.controllers:
            data.pop("controller_mac", None)
        self.save_path(data)

    def save_path(self, data: dict) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(payload)
        os.replace(tmp, CONFIG_PATH)
        try:
            shutil.copy2(CONFIG_PATH, CONFIG_BACKUP)
        except OSError:
            pass


def _apply_env_overrides(cfg: Config) -> None:
    raw = os.environ.get("NGC_IDLE_SLEEP_S", "").strip()
    if raw:
        cfg.idle_sleep_s = max(0.0, float(raw))
    if os.environ.get("NGC_IDLE_SLEEP", "").strip().lower() in {"0", "false", "no", "off"}:
        cfg.idle_sleep_s = 0.0
