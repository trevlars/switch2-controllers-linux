"""High-level Switch 2 controller built on the raw L2CAP ATT client.

Supports the NSO GameCube pad, Pro Controller 2, and Joy-Con 2. The connect
handshake discovers the GATT layout dynamically (so handles need not be
hard-coded per model), reads calibration, decodes input reports, and exposes
LED / vibration control.

Per-model differences handled here:
  * Triggers   - GameCube has true analog L/R; others report digital ZL/ZR.
  * Vibration  - GameCube has no HD actuator (uses safe presets); Pro / Joy-Con
                 drive the real HD-rumble characteristic.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from . import att
from . import protocol as P

logger = logging.getLogger(__name__)

# Fallback handles for the NSO GameCube pad if dynamic discovery fails. Other
# models resolve their handles via discovery (see _resolve_handles).
DEFAULT_HANDLE_INPUT_REPORT = 0x000A
DEFAULT_HANDLE_INPUT_REPORT_CCCD = 0x000B
DEFAULT_HANDLE_VIBRATION = 0x0012
DEFAULT_HANDLE_COMMAND_WRITE = 0x0014
DEFAULT_HANDLE_COMMAND_RESPONSE = 0x001A
DEFAULT_HANDLE_COMMAND_RESPONSE_CCCD = 0x001B

InputCallback = Callable[["SwitchController", P.InputReport], None]


class SwitchController:
    def __init__(self, mac: str, adapter: str):
        self.mac = mac
        self.adapter = adapter
        self.att = att.ATTClient(mac, adapter, dst_type=att.LE_PUBLIC)
        self.info: Optional[P.ControllerInfo] = None
        self.left_calib: Optional[P.StickCalibration] = None
        self.right_calib: Optional[P.StickCalibration] = None
        self.trigger_neutral = (P.GC_TRIGGER_DEFAULT_NEUTRAL, P.GC_TRIGGER_DEFAULT_NEUTRAL)
        self.battery_mv: Optional[int] = None

        # Resolved GATT handles (start with GameCube defaults, refined by discovery).
        self.h_input = DEFAULT_HANDLE_INPUT_REPORT
        self.h_input_cccd = DEFAULT_HANDLE_INPUT_REPORT_CCCD
        self.h_cmd_write = DEFAULT_HANDLE_COMMAND_WRITE
        self.h_cmd_resp = DEFAULT_HANDLE_COMMAND_RESPONSE
        self.h_cmd_resp_cccd = DEFAULT_HANDLE_COMMAND_RESPONSE_CCCD
        self.h_vibration = DEFAULT_HANDLE_VIBRATION

        self._cmd_lock = threading.Lock()
        self._cmd_event = threading.Event()
        self._cmd_response: Optional[bytes] = None
        self._vibration_packet_id = 0
        self._last_rumble_ts = 0.0
        self._last_rumble_mag = 0.0

        # HD-rumble worker (Pro / Joy-Con only): keeps re-sending the motor
        # packet so effects sustain for as long as the game holds them.
        self._hd_target: tuple[float, float] = (0.0, 0.0)
        self._hd_run = False
        self._hd_thread: Optional[threading.Thread] = None
        self._hd_dirty = threading.Event()

        self.input_callback: Optional[InputCallback] = None
        self.disconnect_callback: Optional[Callable[[], None]] = None
        self.last_input_at: float = 0.0

        self.att.notification_cb = self._on_notification
        self.att.disconnect_cb = self._on_disconnect

    # ------------------------------------------------------------------ #
    # Identity                                                            #
    # ------------------------------------------------------------------ #

    @property
    def product_id(self) -> int:
        return self.info.product_id if self.info else P.NSO_GAMECUBE_PID

    @property
    def has_hd_rumble(self) -> bool:
        return P.has_hd_rumble(self.product_id)

    @property
    def has_analog_triggers(self) -> bool:
        return P.has_analog_triggers(self.product_id)

    @property
    def name(self) -> str:
        return self.info.name if self.info else "Switch 2 Controller"

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def connect(self, timeout: float = 10.0) -> bool:
        ok, _ = self.att.connect(timeout=timeout)
        return ok

    def close(self) -> None:
        self._hd_run = False
        self._hd_dirty.set()
        self.att.close()

    @property
    def is_connected(self) -> bool:
        return self.att.is_connected

    def _resolve_handles(self) -> None:
        """Discover the GATT table and map the characteristics we use by UUID.

        Keeps the GameCube defaults if a characteristic is missing so a partial
        discovery never makes things worse."""
        try:
            services = self.att.discover_all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("GATT discovery failed (%s); using default handles", exc)
            return

        by_uuid: dict[str, att.Characteristic] = {}
        for svc in services:
            for ch in svc.characteristics:
                by_uuid[ch.uuid] = ch

        def val(uuid: str, default: int) -> int:
            ch = by_uuid.get(uuid)
            return ch.value_handle if ch else default

        def cccd(uuid: str, default: int) -> int:
            ch = by_uuid.get(uuid)
            return ch.cccd_handle if ch and ch.cccd_handle else default

        self.h_input = val(P.INPUT_REPORT_UUID, self.h_input)
        self.h_input_cccd = cccd(P.INPUT_REPORT_UUID, self.h_input_cccd)
        self.h_cmd_write = val(P.COMMAND_WRITE_UUID, self.h_cmd_write)
        self.h_cmd_resp = val(P.COMMAND_RESPONSE_UUID, self.h_cmd_resp)
        self.h_cmd_resp_cccd = cccd(P.COMMAND_RESPONSE_UUID, self.h_cmd_resp_cccd)
        logger.debug(
            "handles input=%#06x/%#06x cmd_w=%#06x cmd_r=%#06x/%#06x",
            self.h_input, self.h_input_cccd, self.h_cmd_write,
            self.h_cmd_resp, self.h_cmd_resp_cccd,
        )
        self._by_uuid = by_uuid

    def _resolve_vibration_handle(self) -> None:
        uuid = P.vibration_uuid_for(self.product_id)
        ch = getattr(self, "_by_uuid", {}).get(uuid)
        if ch:
            self.h_vibration = ch.value_handle
        logger.debug("vibration handle=%#06x (uuid %s)", self.h_vibration, uuid)

    def enable_commands(self) -> None:
        """Subscribe to the command-response characteristic so write_command
        can correlate replies. Required before any command."""
        self.att.subscribe(self.h_cmd_resp_cccd, True)

    def initialize(self, player: int = 1) -> None:
        """Run the full handshake after a successful connect."""
        self._resolve_handles()
        self._retry("enable commands", self.enable_commands)

        self.info = self._retry("read info", self.read_controller_info)
        logger.info("identified %s serial=%s", self.info.name, self.info.serial_number)
        self._resolve_vibration_handle()

        self._read_calibration()

        self._retry("player LEDs", lambda: self.set_player_leds(player))
        self._retry("vibration test", lambda: self.play_vibration_preset(0x03))
        self._retry("enable features", lambda: self.enable_features(0x03 | P.FEATURE_MOTION))

        if self.has_hd_rumble:
            self._start_hd_worker()

        self._retry("input notifications", lambda: self.att.subscribe(self.h_input_cccd, True))
        self.last_input_at = time.monotonic()
        logger.info("input notifications enabled")

    def _retry(self, label: str, fn, attempts: int = 3, delay: float = 0.1):
        last_exc: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt + 1 < attempts:
                    time.sleep(delay)
        raise RuntimeError(f"{label} failed after {attempts} tries: {last_exc}") from last_exc

    # ------------------------------------------------------------------ #
    # Notification handling                                               #
    # ------------------------------------------------------------------ #

    def _on_notification(self, handle: int, data: bytes) -> None:
        if handle == self.h_input:
            report = P.InputReport.parse(data)
            self.battery_mv = report.battery_mv
            self.last_input_at = time.monotonic()
            if self.input_callback is not None:
                self.input_callback(self, report)
        elif handle == self.h_cmd_resp:
            self._cmd_response = data
            self._cmd_event.set()

    def _on_disconnect(self) -> None:
        self._hd_run = False
        self._hd_dirty.set()
        if self.disconnect_callback is not None:
            self.disconnect_callback()

    # ------------------------------------------------------------------ #
    # Commands                                                            #
    # ------------------------------------------------------------------ #

    def write_command(self, command_id: int, subcommand_id: int, data: bytes = b"", timeout: float = 2.0) -> bytes:
        buf = P.build_command(command_id, subcommand_id, data)
        with self._cmd_lock:
            self._cmd_event.clear()
            self._cmd_response = None
            self.att.write_command(self.h_cmd_write, buf)
            if not self._cmd_event.wait(timeout):
                raise TimeoutError(f"command {command_id:#04x}/{subcommand_id:#04x} timed out")
            resp = self._cmd_response
        if len(resp) < 8 or resp[0] != command_id or resp[1] != 0x01:
            raise RuntimeError(f"unexpected response: {P.to_hex(resp)}")
        return resp[8:]

    def read_memory(self, length: int, address: int) -> bytes:
        if length > 0x4F:
            raise ValueError("max read size is 0x4F")
        payload = length.to_bytes() + b"\x7e\x00\x00" + address.to_bytes(4, "little")
        data = self.write_command(P.COMMAND_MEMORY, P.SUBCOMMAND_MEMORY_READ, payload)
        if data[0] != length or P.decodeu(data[4:8]) != address:
            raise RuntimeError(f"unexpected read response: {P.to_hex(data)}")
        return data[8:]

    def read_controller_info(self) -> P.ControllerInfo:
        return P.ControllerInfo.from_bytes(self.read_memory(0x40, P.ADDRESS_CONTROLLER_INFO))

    def _read_calibration(self) -> None:
        self.left_calib = self._read_stick(P.CALIBRATION_USER_JOYSTICK_1, P.CALIBRATION_JOYSTICK_1)
        self.right_calib = self._read_stick(P.CALIBRATION_USER_JOYSTICK_2, P.CALIBRATION_JOYSTICK_2)
        if self.has_analog_triggers:
            try:
                cal = self.read_memory(0x02, P.CALIBRATION_GC_TRIGGERS)
                if cal and cal[0] not in (0x00, 0xFF):
                    self.trigger_neutral = (cal[0], cal[1])
            except Exception as exc:  # noqa: BLE001
                logger.warning("GC trigger calibration read failed: %s", exc)
        logger.info("calib L=%s R=%s triggers=%s", self.left_calib, self.right_calib, self.trigger_neutral)

    def _read_stick(self, user_addr: int, factory_addr: int) -> P.StickCalibration:
        data = self.read_memory(0x0B, user_addr)
        if P.decodeu(data[:3]) == 0xFFFFFF:
            data = self.read_memory(0x0B, factory_addr)
        return P.StickCalibration.from_bytes(data)

    def enable_features(self, flags: int) -> None:
        payload = flags.to_bytes().ljust(4, b"\x00")
        self.write_command(P.COMMAND_FEATURE, P.SUBCOMMAND_FEATURE_INIT, payload)
        self.write_command(P.COMMAND_FEATURE, P.SUBCOMMAND_FEATURE_ENABLE, payload)

    def bond(self) -> None:
        """Store this host's address + the protocol LTK on the controller so it
        will reconnect to us without re-entering pairing mode."""
        mac_le = P.mac_to_le_bytes(self.adapter)
        self.write_command(P.COMMAND_PAIR, P.SUBCOMMAND_PAIR_SET_MAC, b"\x00\x02" + mac_le + mac_le)
        self.write_command(P.COMMAND_PAIR, P.SUBCOMMAND_PAIR_LTK1, P.PAIR_LTK1)
        self.write_command(P.COMMAND_PAIR, P.SUBCOMMAND_PAIR_LTK2, P.PAIR_LTK2)
        self.write_command(P.COMMAND_PAIR, P.SUBCOMMAND_PAIR_FINISH, b"\x00")
        logger.info("bonded to adapter %s", self.adapter)

    def set_player_leds(self, player: int) -> None:
        player = min(max(player, 1), 8)
        value = P.LED_PATTERN[player]
        self.write_command(P.COMMAND_LEDS, P.SUBCOMMAND_LEDS_SET_PLAYER, value.to_bytes().ljust(4, b"\x00"))

    def play_vibration_preset(self, preset_id: int) -> None:
        self.write_command(P.COMMAND_VIBRATION, P.SUBCOMMAND_VIBRATION_PLAY_PRESET, preset_id.to_bytes().ljust(4, b"\x00"))

    # ------------------------------------------------------------------ #
    # Vibration                                                          #
    # ------------------------------------------------------------------ #

    def _write_motor(self, vib: P.VibrationData) -> None:
        """Write one HD-rumble motor packet. Each block holds three consecutive
        sub-frame vibration samples; fill all three with the same waveform so the
        actuator runs continuously instead of pulsing at ~1/3 duty. Pro
        Controller takes a duplicated block (left + right motors); Joy-Con takes
        a single block."""
        header = (0x50 + (self._vibration_packet_id & 0x0F)).to_bytes()
        sample = vib.to_bytes()
        block = header + sample + sample + sample
        if self.product_id == P.PRO_CONTROLLER2_PID:
            payload = b"\x00" + block + block
        else:
            payload = b"\x00" + block
        self.att.write_command(self.h_vibration, payload)
        self._vibration_packet_id += 1

    def _start_hd_worker(self) -> None:
        if self._hd_thread and self._hd_thread.is_alive():
            return
        self._hd_run = True
        self._hd_dirty.clear()
        self._hd_thread = threading.Thread(target=self._hd_loop, daemon=True)
        self._hd_thread.start()

    # Resonant low-frequency band for this actuator. Driving a fixed frequency
    # and only scaling amplitude gives a monotonic, natural-feeling rumble;
    # changing frequency by magnitude felt uneven because off-resonance drive is
    # far weaker for the same amplitude.
    HD_LF_FREQ = 0x0E1

    @classmethod
    def _hd_waveform(cls, strong: float, weak: float) -> P.VibrationData:
        """Map dual-rumble magnitudes to a natural-feeling HD waveform.

        Tuning (Pro Controller 2): drive only the low-frequency resonant band
        (the high band reads as a harsh buzz) and scale amplitude with the
        combined magnitude. The weak motor folds in at reduced weight.
        """
        mag = min(1.0, strong + weak * 0.5)
        return P.VibrationData(lf_freq=cls.HD_LF_FREQ, lf_amp=int(mag * 0x3FF))

    def _hd_loop(self) -> None:
        """Continuously drive the HD motor while a force-feedback effect is
        active. Re-sends at ~60 Hz (matching the console) so the rumble sustains
        smoothly; idles on an event when there is nothing to play."""
        active = False
        while self._hd_run:
            strong, weak = self._hd_target
            if strong <= 0.001 and weak <= 0.001:
                if active:
                    try:
                        self._write_motor(P.VibrationData())  # stop
                    except Exception:  # noqa: BLE001
                        pass
                    active = False
                self._hd_dirty.wait(timeout=1.0)
                self._hd_dirty.clear()
                continue
            try:
                self._write_motor(self._hd_waveform(strong, weak))
            except Exception:  # noqa: BLE001
                pass
            active = True
            time.sleep(0.016)

    def set_rumble(self, strong: float, weak: float) -> None:
        """Drive vibration from normalised 0..1 force-feedback magnitudes.

        HD-rumble controllers (Pro / Joy-Con) get the real motor packet via the
        sustaining worker. The GameCube pad has no HD actuator (its motor
        characteristic powers it off) and can only replay built-in presets,
        which have no amplitude control: preset 2 is a strong sustained buzz,
        preset 3 a light tap.

        To avoid a constant strong slam when a game holds rumble, this is
        edge-driven: a fresh rumble onset (e.g. Steam's ping, an impact) fires
        the strong preset once, while a *sustained* effect only emits gentle
        light pulses at a relaxed cadence. So single events feel punchy without
        continuous rumble being overwhelming.
        """
        if self.has_hd_rumble:
            self._hd_target = (max(0.0, strong), max(0.0, weak))
            self._hd_dirty.set()
            return

        magnitude = max(strong, weak)
        now = time.monotonic()
        prev = self._last_rumble_mag
        self._last_rumble_mag = magnitude
        if magnitude <= 0.02:
            return  # presets are one-shot; nothing to stop

        rising = prev <= 0.02  # transition from idle -> a new rumble event
        if rising:
            # New event: a solid buzz. Low-magnitude one-shots (like Steam's
            # ping) still get the strong preset so they are clearly felt.
            self._last_rumble_ts = now
            self.play_vibration_preset(0x02 if magnitude >= 0.15 else 0x03)
        elif now - self._last_rumble_ts >= 0.35:
            # Held/continuous rumble: gentle periodic light tap, never the
            # strong preset, so sustained effects don't buzz constantly.
            self._last_rumble_ts = now
            self.play_vibration_preset(0x03)

    # ------------------------------------------------------------------ #

    def calibrated_input(self, report: P.InputReport):
        """Return (left_xy, right_xy floats, lt, rt 0-255) from a report."""
        lx, ly = self.left_calib.apply(report.left_stick_raw) if self.left_calib else (0.0, 0.0)
        rx, ry = self.right_calib.apply(report.right_stick_raw) if self.right_calib else (0.0, 0.0)
        if self.has_analog_triggers:
            lt = P.normalize_trigger(report.left_trigger_raw, self.trigger_neutral[0])
            rt = P.normalize_trigger(report.right_trigger_raw, self.trigger_neutral[1])
        else:
            # Digital ZL/ZR -> full-scale trigger axis.
            lt = 255 if report.buttons & P.SWITCH_BUTTONS["ZL"] else 0
            rt = 255 if report.buttons & P.SWITCH_BUTTONS["ZR"] else 0
        return (lx, ly), (rx, ry), lt, rt


# Backwards-compatible alias (older tools import GameCubeController).
GameCubeController = SwitchController
