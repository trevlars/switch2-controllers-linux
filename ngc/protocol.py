"""Switch 2 BLE controller protocol: identifiers, GATT UUIDs, commands, and
input-report parsing.

Protocol knowledge distilled from:
  - Nadeflore/switch2-controllers (GATT UUIDs, command framing, report layout)
  - ndeadly's switch2 input viewer (GameCube analog-trigger specifics)
  - bitaxislabs Switch 2 BLE protocol writeup

This module is transport-agnostic and has no Bluetooth or OS dependencies so it
can be unit-tested anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# --------------------------------------------------------------------------- #
# Controller identification                                                    #
# --------------------------------------------------------------------------- #

NINTENDO_VENDOR_ID = 0x057E
# BLE advertising manufacturer-data company id used by Switch 2 controllers.
NINTENDO_COMPANY_ID = 0x0553

JOYCON2_RIGHT_PID = 0x2066
JOYCON2_LEFT_PID = 0x2067
PRO_CONTROLLER2_PID = 0x2069
NSO_GAMECUBE_PID = 0x2073

CONTROLLER_NAMES = {
    JOYCON2_RIGHT_PID: "Joy-Con 2 (Right)",
    JOYCON2_LEFT_PID: "Joy-Con 2 (Left)",
    PRO_CONTROLLER2_PID: "Pro Controller 2",
    NSO_GAMECUBE_PID: "NSO GameCube Controller",
}

SUPPORTED_PIDS = set(CONTROLLER_NAMES)

# Controllers with true HD-rumble linear actuators. The NSO GameCube pad does
# NOT have one (writing its motor characteristic powers it off), so it uses the
# built-in vibration presets instead.
HD_RUMBLE_PIDS = {JOYCON2_RIGHT_PID, JOYCON2_LEFT_PID, PRO_CONTROLLER2_PID}

# Only the GameCube pad exposes true analog L/R triggers in the input report.
# Everything else reports ZL/ZR as digital buttons.
ANALOG_TRIGGER_PIDS = {NSO_GAMECUBE_PID}


def has_hd_rumble(pid: int) -> bool:
    return pid in HD_RUMBLE_PIDS


def has_analog_triggers(pid: int) -> bool:
    return pid in ANALOG_TRIGGER_PIDS

# --------------------------------------------------------------------------- #
# GATT characteristic UUIDs                                                     #
# --------------------------------------------------------------------------- #

INPUT_REPORT_UUID = "ab7de9be-89fe-49ad-828f-118f09df7fd2"
COMMAND_WRITE_UUID = "649d4ac9-8eb7-4e6c-af44-1ea54fe5f005"
COMMAND_RESPONSE_UUID = "c765a961-d9d8-4d36-a20a-5315b111836a"

VIBRATION_WRITE_PRO_CONTROLLER_UUID = "cc483f51-9258-427d-a939-630c31f72b05"
VIBRATION_WRITE_JOYCON_R_UUID = "fa19b0fb-cd1f-46a7-84a1-bbb09e00c149"
VIBRATION_WRITE_JOYCON_L_UUID = "289326cb-a471-485d-a8f4-240c14f18241"


def vibration_uuid_for(pid: int) -> str:
    """Return the HD-rumble characteristic UUID for a controller PID."""
    if pid == JOYCON2_LEFT_PID:
        return VIBRATION_WRITE_JOYCON_L_UUID
    if pid == JOYCON2_RIGHT_PID:
        return VIBRATION_WRITE_JOYCON_R_UUID
    # Pro Controller 2 and the GameCube pad share the "pro" vibration char.
    return VIBRATION_WRITE_PRO_CONTROLLER_UUID

# --------------------------------------------------------------------------- #
# Commands / subcommands                                                        #
# --------------------------------------------------------------------------- #

COMMAND_LEDS = 0x09
SUBCOMMAND_LEDS_SET_PLAYER = 0x07

COMMAND_VIBRATION = 0x0A
SUBCOMMAND_VIBRATION_PLAY_PRESET = 0x02

COMMAND_MEMORY = 0x02
SUBCOMMAND_MEMORY_READ = 0x04

COMMAND_PAIR = 0x15
SUBCOMMAND_PAIR_SET_MAC = 0x01
SUBCOMMAND_PAIR_LTK1 = 0x04
SUBCOMMAND_PAIR_LTK2 = 0x02
SUBCOMMAND_PAIR_FINISH = 0x03

# Fixed LTK halves the Switch 2 protocol expects during bonding (each prefixed
# with a 0x00 byte). The controller stores the host address + this key so it can
# reconnect to us without re-entering pairing mode.
PAIR_LTK1 = bytes([0x00, 0xEA, 0xBD, 0x47, 0x13, 0x89, 0x35, 0x42, 0xC6, 0x79, 0xEE, 0x07, 0xF2, 0x53, 0x2C, 0x6C, 0x31])
PAIR_LTK2 = bytes([0x00, 0x40, 0xB0, 0x8A, 0x5F, 0xCD, 0x1F, 0x9B, 0x41, 0x12, 0x5C, 0xAC, 0xC6, 0x3F, 0x38, 0xA0, 0x73])


def mac_to_le_bytes(mac: str) -> bytes:
    """Return a 6-byte little-endian representation of a MAC string."""
    return bytes.fromhex(mac.replace(":", ""))[::-1]


def mac_to_int(mac: str) -> int:
    """Return the host MAC as a big-endian integer (Switch 2 reconnect field)."""
    return int.from_bytes(bytes(int(b, 16) for b in mac.split(":")), "big")


def reconnect_mac_from_advertisement(adv) -> Optional[int]:
    """Parse the bonded-host MAC embedded in a Switch 2 advertisement.

    Returns 0 when the controller is in pairing mode, otherwise the host MAC it
    will wake for (big-endian integer, same encoding as ``mac_to_int``).
    """
    manu = getattr(adv, "manufacturer_data", {}).get(NINTENDO_COMPANY_ID)
    if not manu or len(manu) < 16:
        return None
    return decodeu(manu[10:16])

COMMAND_FEATURE = 0x0C
SUBCOMMAND_FEATURE_INIT = 0x02
SUBCOMMAND_FEATURE_ENABLE = 0x04

FEATURE_MOTION = 0x04
FEATURE_MOUSE = 0x10
FEATURE_MAGNETOMETER = 0x80

# --------------------------------------------------------------------------- #
# Controller memory addresses                                                   #
# --------------------------------------------------------------------------- #

ADDRESS_CONTROLLER_INFO = 0x00013000
CALIBRATION_JOYSTICK_1 = 0x000130A8
CALIBRATION_JOYSTICK_2 = 0x000130E8
CALIBRATION_USER_JOYSTICK_1 = 0x001FC042
CALIBRATION_USER_JOYSTICK_2 = 0x001FC062
# GameCube analog-trigger calibration (2 bytes: left neutral, right neutral).
CALIBRATION_GC_TRIGGERS = 0x00013140

# Player-LED bit patterns matching the Switch, for up to 8 players.
LED_PATTERN = {1: 0x01, 2: 0x03, 3: 0x07, 4: 0x0F, 5: 0x09, 6: 0x05, 7: 0x0D, 8: 0x06}

# Default GameCube trigger neutral when factory calibration is unreadable
# (value from BlueRetro: neutral ~30, full press ~195/255).
GC_TRIGGER_DEFAULT_NEUTRAL = 30

# --------------------------------------------------------------------------- #
# Button bitmask (32-bit LE, report bytes [4:8])                                #
# --------------------------------------------------------------------------- #

SWITCH_BUTTONS = {
    "Y": 0x00000001,
    "X": 0x00000002,
    "B": 0x00000004,
    "A": 0x00000008,
    "SR_R": 0x00000010,
    "SL_R": 0x00000020,
    "R": 0x00000040,
    "ZR": 0x00000080,
    "MINUS": 0x00000100,
    "PLUS": 0x00000200,
    "R_STK": 0x00000400,
    "L_STK": 0x00000800,
    "HOME": 0x00001000,
    "CAPTURE": 0x00002000,
    "C": 0x00004000,
    "DOWN": 0x00010000,
    "UP": 0x00020000,
    "RIGHT": 0x00040000,
    "LEFT": 0x00080000,
    "SR_L": 0x00100000,
    "SL_L": 0x00200000,
    "L": 0x00400000,
    "ZL": 0x00800000,
    "GR": 0x01000000,
    "GL": 0x02000000,
}


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #


def decodeu(data: bytes) -> int:
    return int.from_bytes(data, byteorder="little", signed=False)


def decodes(data: bytes) -> int:
    return int.from_bytes(data, byteorder="little", signed=True)


def to_hex(buffer: bytes) -> str:
    return " ".join(f"{b:02x}" for b in buffer)


def get_stick_xy(data: bytes) -> tuple[int, int]:
    """Decode 3 packed bytes into two 12-bit (0..4095) stick axis values."""
    value = decodeu(data)
    return value & 0xFFF, value >> 12


def reverse_bits(n: int, no_of_bits: int) -> int:
    result = 0
    for _ in range(no_of_bits):
        result = (result << 1) | (n & 1)
        n >>= 1
    return result


# --------------------------------------------------------------------------- #
# Calibration                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class StickCalibration:
    center: tuple[int, int]
    max: tuple[int, int]
    min: tuple[int, int]

    @classmethod
    def from_bytes(cls, data: bytes) -> "StickCalibration":
        return cls(
            center=get_stick_xy(data[0:3]),
            max=get_stick_xy(data[3:6]),
            min=get_stick_xy(data[6:9]),
        )

    @staticmethod
    def _axis(raw: int, center: int, max_abs: int, min_abs: int, deadzone: int) -> float:
        signed = raw - center
        if signed > deadzone:
            return min(signed / max_abs, 1.0) if max_abs else 0.0
        if signed < -deadzone:
            return -min(-signed / min_abs, 1.0) if min_abs else 0.0
        return 0.0

    def apply(self, raw: tuple[int, int], deadzone: int = 0) -> tuple[float, float]:
        return (
            self._axis(raw[0], self.center[0], self.max[0], self.min[0], deadzone),
            self._axis(raw[1], self.center[1], self.max[1], self.min[1], deadzone),
        )


def normalize_trigger(raw: int, neutral: int) -> int:
    """Normalize a raw 0..255 analog-trigger reading to 0..255 using its
    neutral (rest) value as the zero point."""
    if neutral >= 255:
        return 0
    val = (raw - neutral) * 255 // (255 - neutral)
    return max(0, min(255, val))


# --------------------------------------------------------------------------- #
# Controller info                                                               #
# --------------------------------------------------------------------------- #


@dataclass
class ControllerInfo:
    serial_number: str
    vendor_id: int
    product_id: int
    colors: tuple[bytes, bytes, bytes, bytes]

    @classmethod
    def from_bytes(cls, data: bytes) -> "ControllerInfo":
        return cls(
            serial_number=data[2:16].decode(errors="replace").rstrip("\x00"),
            vendor_id=decodeu(data[18:20]),
            product_id=decodeu(data[20:22]),
            colors=(data[25:28], data[28:31], data[31:34], data[34:37]),
        )

    @property
    def name(self) -> str:
        return CONTROLLER_NAMES.get(self.product_id, f"Switch 2 Controller {self.product_id:#06x}")


# --------------------------------------------------------------------------- #
# Input report                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class InputReport:
    """Parsed 63-byte input report (INPUT_REPORT characteristic)."""

    raw: bytes
    timestamp: int
    buttons: int
    left_stick_raw: tuple[int, int]
    right_stick_raw: tuple[int, int]
    battery_mv: int
    accel: tuple[int, int, int]
    gyro: tuple[int, int, int]
    left_trigger_raw: int
    right_trigger_raw: int

    @classmethod
    def parse(cls, data: bytes) -> "InputReport":
        return cls(
            raw=bytes(data),
            timestamp=decodeu(data[0:4]),
            buttons=decodeu(data[4:8]),
            left_stick_raw=get_stick_xy(data[10:13]),
            right_stick_raw=get_stick_xy(data[13:16]),
            battery_mv=decodeu(data[0x1F:0x21]),
            accel=(decodes(data[0x30:0x32]), decodes(data[0x32:0x34]), decodes(data[0x34:0x36])),
            gyro=(decodes(data[0x36:0x38]), decodes(data[0x38:0x3A]), decodes(data[0x3A:0x3C])),
            left_trigger_raw=data[0x3C] if len(data) > 0x3C else 0,
            right_trigger_raw=data[0x3D] if len(data) > 0x3D else 0,
        )

    def pressed(self) -> list[str]:
        return [name for name, mask in SWITCH_BUTTONS.items() if self.buttons & mask]


# --------------------------------------------------------------------------- #
# Vibration                                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class VibrationData:
    lf_freq: int = 0x0E1
    lf_en_tone: bool = False
    lf_amp: int = 0x000
    hf_freq: int = 0x1E1
    hf_en_tone: bool = False
    hf_amp: int = 0x000

    def to_bytes(self) -> bytes:
        value = 0
        value |= self.lf_freq & 0x1FF
        value |= int(self.lf_en_tone) << 9
        value |= (self.lf_amp & 0x3FF) << 10
        value |= (self.hf_freq & 0x1FF) << 20
        value |= int(self.hf_en_tone) << 29
        value |= (self.hf_amp & 0x3FF) << 30
        return value.to_bytes(length=5, byteorder="little")


def build_command(command_id: int, subcommand_id: int, data: bytes = b"") -> bytes:
    """Frame a controller command (the shared 0x91 protocol)."""
    return (
        command_id.to_bytes()
        + b"\x91\x01"
        + subcommand_id.to_bytes()
        + b"\x00"
        + len(data).to_bytes()
        + b"\x00\x00"
        + data
    )
