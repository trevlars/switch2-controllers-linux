"""Linux virtual gamepad (uinput) for Switch 2 controllers.

Presents a standard dual-stick gamepad with analog triggers (GameCube) or
digital ZL/ZR (Pro / Joy-Con) so SDL, Steam Input, and emulators recognise
pads without custom kernel support.
"""

from __future__ import annotations

import logging
import struct
import threading
from typing import Callable, Optional

from evdev import UInput, AbsInfo, ecodes as e

from . import protocol as P
from .motion_evdev import phys_for_mac

logger = logging.getLogger(__name__)

# struct input_event on 64-bit Linux: timeval(2*long) + type + code + value
_EVENT_FMT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)

# Axis ranges.
STICK_MIN, STICK_MAX = -32768, 32767
TRIGGER_MIN, TRIGGER_MAX = 0, 255

# --------------------------------------------------------------------------- #
# Evdev maps                                                                   #
#                                                                             #
# BLE input uses the unified Switch 2 bitmask in protocol.SWITCH_BUTTONS      #
# (Nadeflore/switch2-controllers, bitaxislabs). On the NSO GameCube pad the   #
# ZR bit (0x80) is physical Z; R bit (0x40) is R trigger full-click. For Steam /
# Switch-style layouts, ZL+ L -> BTN_TL (left bumper) and Z+ R click -> BTN_TR
# (right bumper). C -> BTN_SELECT (minus). Analog L/R stay on trigger axes.      #
#                                                                             #
# Face buttons use semantic evdev positions (A=SOUTH, B=EAST, X=WEST, Y=NORTH).
# BTN_C (306) sits between B and the face cluster, so SDL's auto gamecontrollerdb
# assigns x:b3/y:b4 to the wrong indices — install-emulator-integration.sh writes
# a corrected mapping for Steam. Dolphin uses WEST/NORTH tokens directly.
#
# NSO GameCube has Start (PLUS bit) and Home (HOME bit), no Select/MINUS.
# Capture/C need misc slots in gamecontrollerdb.
# --------------------------------------------------------------------------- #

PRO_BUTTON_MAP = {
    "A": e.BTN_SOUTH,
    "B": e.BTN_EAST,
    "X": e.BTN_WEST,
    "Y": e.BTN_NORTH,
    "L": e.BTN_TL,
    "R": e.BTN_TR,
    "ZL": e.BTN_TL2,
    "ZR": e.BTN_TR2,
    "PLUS": e.BTN_START,
    "MINUS": e.BTN_SELECT,
    "HOME": e.BTN_MODE,
    "CAPTURE": e.BTN_Z,
    "C": e.BTN_C,
    "L_STK": e.BTN_THUMBL,
    "R_STK": e.BTN_THUMBR,
}

# ZL/Z and L/R click share shoulder slots so Steam and emulators see standard bumpers.
GAMECUBE_BUTTON_MAP = {
    "A": e.BTN_SOUTH,
    "B": e.BTN_EAST,
    # NSO GC face layout: X is left (north on diamond), Y is top (west on diamond).
    "X": e.BTN_NORTH,
    "Y": e.BTN_WEST,
    "L": e.BTN_TL,
    "R": e.BTN_TR,
    "ZL": e.BTN_TL,       # left bumper (with L click)
    "ZR": e.BTN_TR,       # right bumper / physical Z (with R click)
    "PLUS": e.BTN_START,   # GC Start; BLE PLUS bit (not MINUS)
    "HOME": e.BTN_MODE,    # GC Nintendo/Home
    "CAPTURE": e.BTN_Z,    # screenshot; b4 -> misc2 in gamecontrollerdb
    "C": e.BTN_SELECT,     # minus / back (GC has no Select; C fills that slot)
    "L_STK": e.BTN_THUMBL,
    "R_STK": e.BTN_THUMBR,
}

DEFAULT_BUTTON_MAP = PRO_BUTTON_MAP


def button_map_for_product(product_id: int) -> dict:
    """Return the default evdev map for a Switch 2 controller PID."""
    if product_id == P.NSO_GAMECUBE_PID:
        return GAMECUBE_BUTTON_MAP
    return PRO_BUTTON_MAP


class SwitchGamepad:
    def __init__(
        self,
        name: str = "NSO GameCube Controller",
        button_map=None,
        product: int = P.NSO_GAMECUBE_PID,
        mac: str = "",
    ):
        self.button_map = button_map or DEFAULT_BUTTON_MAP
        keys = sorted(set(self.button_map.values()))

        capabilities = {
            e.EV_KEY: keys,
            e.EV_ABS: [
                (e.ABS_X, AbsInfo(0, STICK_MIN, STICK_MAX, 0, 0, 0)),
                (e.ABS_Y, AbsInfo(0, STICK_MIN, STICK_MAX, 0, 0, 0)),
                (e.ABS_RX, AbsInfo(0, STICK_MIN, STICK_MAX, 0, 0, 0)),
                (e.ABS_RY, AbsInfo(0, STICK_MIN, STICK_MAX, 0, 0, 0)),
                (e.ABS_Z, AbsInfo(0, TRIGGER_MIN, TRIGGER_MAX, 0, 0, 0)),
                (e.ABS_RZ, AbsInfo(0, TRIGGER_MIN, TRIGGER_MAX, 0, 0, 0)),
                (e.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
                (e.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0)),
            ],
            e.EV_FF: [e.FF_RUMBLE, e.FF_PERIODIC, e.FF_CONSTANT, e.FF_GAIN],
        }

        phys = phys_for_mac(mac) if mac else "py-evdev-uinput"
        self.ui = UInput(
            capabilities,
            name=name,
            vendor=P.NINTENDO_VENDOR_ID,
            product=product,
            version=0x0100,
            bustype=e.BUS_BLUETOOTH,
            phys=phys,
        )
        logger.info("created virtual gamepad: %s", self.ui.device.path if self.ui.device else name)

        self._last_keys: dict[int, int] = {}
        self._last_abs: dict[int, int] = {}

        self.rumble_cb: Optional[Callable[[float, float], None]] = None
        self._effects: dict[int, tuple[int, int]] = {}
        self._ff_running = True
        self._ff_thread = threading.Thread(target=self._ff_loop, daemon=True)
        self._ff_thread.start()

    @staticmethod
    def _scale_stick(value: float) -> int:
        v = int(value * STICK_MAX)
        return max(STICK_MIN, min(STICK_MAX, v))

    def _emit_key(self, code: int, pressed: int) -> bool:
        if self._last_keys.get(code) != pressed:
            self.ui.write(e.EV_KEY, code, pressed)
            self._last_keys[code] = pressed
            return True
        return False

    def _emit_abs(self, code: int, value: int) -> bool:
        if self._last_abs.get(code) != value:
            self.ui.write(e.EV_ABS, code, value)
            self._last_abs[code] = value
            return True
        return False

    def update(
        self,
        buttons: int,
        left_stick: tuple[float, float],
        right_stick: tuple[float, float],
        left_trigger: int,
        right_trigger: int,
        *,
        emit_dpad: bool = True,
    ) -> None:
        changed = False

        # Emit every mapped key each frame; OR switch names that share a code
        # (GC R trigger click and Z both use Shoulder R / BTN_TR).
        key_states = {code: 0 for code in set(self.button_map.values())}
        for switch_name, key_code in self.button_map.items():
            mask = P.SWITCH_BUTTONS.get(switch_name, 0)
            if mask and (buttons & mask):
                key_states[key_code] = 1
        for key_code, pressed in key_states.items():
            changed |= self._emit_key(key_code, pressed)

        dpad_x = (1 if buttons & P.SWITCH_BUTTONS["RIGHT"] else 0) - (
            1 if buttons & P.SWITCH_BUTTONS["LEFT"] else 0
        )
        dpad_y = (1 if buttons & P.SWITCH_BUTTONS["DOWN"] else 0) - (
            1 if buttons & P.SWITCH_BUTTONS["UP"] else 0
        )
        changed |= self._emit_abs(e.ABS_HAT0X, dpad_x if emit_dpad else 0)
        changed |= self._emit_abs(e.ABS_HAT0Y, dpad_y if emit_dpad else 0)

        changed |= self._emit_abs(e.ABS_X, self._scale_stick(left_stick[0]))
        changed |= self._emit_abs(e.ABS_Y, -self._scale_stick(left_stick[1]))
        changed |= self._emit_abs(e.ABS_RX, self._scale_stick(right_stick[0]))
        changed |= self._emit_abs(e.ABS_RY, -self._scale_stick(right_stick[1]))
        changed |= self._emit_abs(e.ABS_Z, max(0, min(255, left_trigger)))
        changed |= self._emit_abs(e.ABS_RZ, max(0, min(255, right_trigger)))

        if changed:
            self.ui.syn()

    def release_all(self) -> None:
        changed = False
        for code in list(self._last_keys):
            changed |= self._emit_key(code, 0)
        for code, neutral in (
            (e.ABS_X, 0),
            (e.ABS_Y, 0),
            (e.ABS_RX, 0),
            (e.ABS_RY, 0),
            (e.ABS_Z, 0),
            (e.ABS_RZ, 0),
            (e.ABS_HAT0X, 0),
            (e.ABS_HAT0Y, 0),
        ):
            changed |= self._emit_abs(code, neutral)
        if changed:
            self.ui.syn()

    def _ff_loop(self) -> None:
        try:
            for event in self.ui.read_loop():
                if not self._ff_running:
                    break
                try:
                    self._handle_ff_event(event.type, event.code, event.value)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    def _handle_ff_event(self, etype: int, code: int, value: int) -> None:
        if etype == e.EV_UINPUT and code == e.UI_FF_UPLOAD:
            upload = self.ui.begin_upload(value)
            effect = upload.effect
            if effect.type == e.FF_RUMBLE:
                r = effect.u.ff_rumble_effect
                self._effects[effect.id] = (r.strong_magnitude, r.weak_magnitude)
            upload.retval = 0
            self.ui.end_upload(upload)
        elif etype == e.EV_UINPUT and code == e.UI_FF_ERASE:
            erase = self.ui.begin_erase(value)
            self._effects.pop(erase.effect_id, None)
            erase.retval = 0
            self.ui.end_erase(erase)
        elif etype == e.EV_FF:
            if self.rumble_cb is None:
                return
            if value == 0:
                self.rumble_cb(0.0, 0.0)
            else:
                strong, weak = self._effects.get(code, (0, 0))
                self.rumble_cb(strong / 65535.0, weak / 65535.0)

    def close(self) -> None:
        self._ff_running = False
        if self.rumble_cb is not None:
            try:
                self.rumble_cb(0.0, 0.0)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.ui.close()
        except Exception:  # noqa: BLE001
            pass


GameCubeGamepad = SwitchGamepad
