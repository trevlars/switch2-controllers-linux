"""Persistent configuration for the bridge.

Supports multiple bonded controllers (for local multiplayer): each entry has a
MAC, an assigned player slot, and a remembered name. Stored as JSON under XDG
config. Legacy single-controller configs are migrated automatically on load.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "nso-gc"
CONFIG_PATH = CONFIG_DIR / "config.json"
CONFIG_BACKUP = CONFIG_DIR / "config.json.bak"


def detect_adapter() -> Optional[str]:
    """Return the first Bluetooth adapter's address from sysfs (no root needed)."""
    base = Path("/sys/class/bluetooth")
    if not base.exists():
        return None
    for hci in sorted(base.iterdir()):
        addr = hci / "address"
        if addr.exists():
            try:
                return addr.read_text().strip().upper()
            except OSError:
                continue
    return None


@dataclass
class ControllerEntry:
    mac: str
    player: int = 1
    name: str = ""
    bonded: bool = False


@dataclass
class Config:
    controllers: list = field(default_factory=list)  # list of {mac, player, name}
    adapter_mac: Optional[str] = None
    button_map: dict = field(default_factory=dict)
    # Rumble: GameCube uses safe presets; Pro/Joy-Con use the real HD motor.
    enable_rumble: bool = True
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
            )
            for i, c in enumerate(self.controllers)
        ]

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
