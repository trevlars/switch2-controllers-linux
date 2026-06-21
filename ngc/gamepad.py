"""Linux virtual gamepad (uinput) for the NSO GameCube controller.

Presents a standard dual-stick gamepad with analog triggers so SDL/Steam Input
and games recognise it without a custom mapping. Outputs an Xbox-style button
layout (the most broadly compatible) while preserving the GameCube's true
analog L/R triggers and C-stick.
"""

from __future__ import annotations

import logging
import struct
import threading
from typing import Callable, Optional

from evdev import UInput, AbsInfo, ecodes as e

from . import protocol as P

logger = logging.getLogger(__name__)

# struct input_event on 64-bit Linux: timeval(2*long) + type + code + value
_EVENT_FMT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)

# Axis ranges.
STICK_MIN, STICK_MAX = -32768, 32767
TRIGGER_MIN, TRIGGER_MAX = 0, 255

# Switch button name -> evdev key. Face buttons use the standard Xbox-style
# evdev positions (A->SOUTH, B->EAST, X->WEST, Y->NORTH). This is what Steam
# Input expects, so the controller's printed labels match Steam's A/B/X/Y. The
# emulator side accounts for the GameCube/Switch labels in its own profile
# (e.g. GC_nso_gamecube.ini maps GameCube A to `Button S`).
DEFAULT_BUTTON_MAP = {
    "A": e.BTN_SOUTH,    # A -> SDL South (Steam A)
    "B": e.BTN_EAST,     # B -> SDL East  (Steam B)
    "X": e.BTN_WEST,     # X -> SDL West  (Steam X)
    "Y": e.BTN_NORTH,    # Y -> SDL North (Steam Y)
    "L": e.BTN_TL,       # left trigger digital click
    "R": e.BTN_TR,       # right trigger digital click
    "ZL": e.BTN_TL2,     # extra shoulder (digital)
    "ZR": e.BTN_TR2,     # GameCube Z (commonly mapped here)
    "PLUS": e.BTN_START,
    "MINUS": e.BTN_SELECT,
    "HOME": e.BTN_MODE,
    "CAPTURE": e.BTN_Z,
    "C": e.BTN_C,        # NSO "C" (GameChat) button
    "L_STK": e.BTN_THUMBL,
    "R_STK": e.BTN_THUMBR,
}


class SwitchGamepad:
    def __init__(
        self,
        name: str = "NSO GameCube Controller",
        button_map=None,
        product: int = P.NSO_GAMECUBE_PID,
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
            # Advertise rumble so SDL/Steam route force-feedback to us.
            e.EV_FF: [e.FF_RUMBLE, e.FF_PERIODIC, e.FF_CONSTANT, e.FF_GAIN],
        }

        # vendor/product identify the controller to SDL; use Nintendo's IDs.
        self.ui = UInput(
            capabilities,
            name=name,
            vendor=P.NINTENDO_VENDOR_ID,
            product=product,
            version=0x0100,
        )
        logger.info("created virtual gamepad: %s", self.ui.device.path if self.ui.device else name)

        self._last_keys: dict[int, int] = {}
        self._last_abs: dict[int, int] = {}

        # Force-feedback: callback(strong 0..1, weak 0..1) wired by the bridge.
        self.rumble_cb: Optional[Callable[[float, float], None]] = None
        self._effects: dict[int, tuple[int, int]] = {}
        self._ff_running = True
        self._ff_thread = threading.Thread(target=self._ff_loop, daemon=True)
        self._ff_thread.start()

    # ------------------------------------------------------------------ #

    @staticmethod
    def _scale_stick(value: float) -> int:
        """Map a calibrated -1.0..1.0 axis to the int16 range."""
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
    ) -> None:
        changed = False

        for switch_name, key_code in self.button_map.items():
            mask = P.SWITCH_BUTTONS.get(switch_name, 0)
            changed |= self._emit_key(key_code, 1 if (buttons & mask) else 0)

        # D-pad -> hat axes
        dpad_x = (1 if buttons & P.SWITCH_BUTTONS["RIGHT"] else 0) - (
            1 if buttons & P.SWITCH_BUTTONS["LEFT"] else 0
        )
        dpad_y = (1 if buttons & P.SWITCH_BUTTONS["DOWN"] else 0) - (
            1 if buttons & P.SWITCH_BUTTONS["UP"] else 0
        )
        changed |= self._emit_abs(e.ABS_HAT0X, dpad_x)
        changed |= self._emit_abs(e.ABS_HAT0Y, dpad_y)

        # Sticks (Y inverted: gamepad convention is up = negative)
        changed |= self._emit_abs(e.ABS_X, self._scale_stick(left_stick[0]))
        changed |= self._emit_abs(e.ABS_Y, -self._scale_stick(left_stick[1]))
        changed |= self._emit_abs(e.ABS_RX, self._scale_stick(right_stick[0]))
        changed |= self._emit_abs(e.ABS_RY, -self._scale_stick(right_stick[1]))

        # Analog triggers
        changed |= self._emit_abs(e.ABS_Z, max(0, min(255, left_trigger)))
        changed |= self._emit_abs(e.ABS_RZ, max(0, min(255, right_trigger)))

        if changed:
            self.ui.syn()

    # ------------------------------------------------------------------ #
    # Force feedback (rumble)                                             #
    # ------------------------------------------------------------------ #

    def _ff_loop(self) -> None:
        """Read FF upload/erase/play events from the uinput fd and translate
        rumble effects into controller vibration via ``rumble_cb``."""
        try:
            for event in self.ui.read_loop():
                if not self._ff_running:
                    break
                try:
                    self._handle_ff_event(event.type, event.code, event.value)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001 - fd closed on shutdown
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


# Backwards-compatible alias (older tools import GameCubeGamepad).
GameCubeGamepad = SwitchGamepad
