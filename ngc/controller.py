"""BLE connection + protocol handshake for a single Switch 2 controller.

Linux-native: uses bleak (BlueZ/D-Bus backend). No Windows dependencies.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from . import protocol as P

logger = logging.getLogger(__name__)

InputCallback = Callable[["Controller", P.InputReport], None]
DisconnectCallback = Callable[["Controller"], Awaitable[None]]


class Controller:
    def __init__(self, device):
        # Accepts a BLEDevice or a bare MAC-address string.
        self.device = device
        self.client: Optional[BleakClient] = None
        self.info: Optional[P.ControllerInfo] = None
        self.left_calib: Optional[P.StickCalibration] = None
        self.right_calib: Optional[P.StickCalibration] = None
        self.trigger_neutral: tuple[int, int] = (
            P.GC_TRIGGER_DEFAULT_NEUTRAL,
            P.GC_TRIGGER_DEFAULT_NEUTRAL,
        )
        self.battery_mv: Optional[int] = None

        self._response_future: Optional[asyncio.Future] = None
        self._vibration_packet_id = 0
        self.input_callback: Optional[InputCallback] = None
        self.disconnect_callback: Optional[DisconnectCallback] = None

    def __repr__(self) -> str:
        name = self.info.name if self.info else "Switch 2 Controller"
        return f"<{name} @ {self.device.address}>"

    # ------------------------------------------------------------------ #
    # Connection lifecycle                                                #
    # ------------------------------------------------------------------ #

    async def connect(self, timeout: float = 30.0) -> None:
        if self.client is not None:
            raise RuntimeError("already connected")

        def _on_disconnect(_client: BleakClient) -> None:
            if self.disconnect_callback is not None:
                asyncio.create_task(self.disconnect_callback(self))

        self.client = BleakClient(
            self.device, timeout=timeout, disconnected_callback=_on_disconnect
        )
        await self.client.connect()
        addr = getattr(self.device, "address", self.device)
        logger.info("connected to %s", addr)

        await self.client.start_notify(P.COMMAND_RESPONSE_UUID, self._on_command_response)

        self.info = await self.read_controller_info()
        logger.info("identified %s (serial %s)", self.info.name, self.info.serial_number)

        await self._read_all_calibration()

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self.client = None

    @property
    def is_connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    # ------------------------------------------------------------------ #
    # Command plumbing                                                    #
    # ------------------------------------------------------------------ #

    def _on_command_response(self, _sender, data: bytearray) -> None:
        if self._response_future and not self._response_future.done():
            self._response_future.set_result(bytes(data))

    async def write_command(
        self, command_id: int, subcommand_id: int, data: bytes = b"", timeout: float = 2.0
    ) -> bytes:
        buf = P.build_command(command_id, subcommand_id, data)
        self._response_future = asyncio.get_running_loop().create_future()
        await self.client.write_gatt_char(P.COMMAND_WRITE_UUID, buf, response=True)
        resp = await asyncio.wait_for(self._response_future, timeout=timeout)
        if len(resp) < 8 or resp[0] != command_id or resp[1] != 0x01:
            raise RuntimeError(f"unexpected command response: {P.to_hex(resp)}")
        return resp[8:]

    async def read_memory(self, length: int, address: int) -> bytes:
        if length > 0x4F:
            raise ValueError("maximum read size is 0x4F bytes")
        payload = length.to_bytes() + b"\x7e\x00\x00" + address.to_bytes(4, "little")
        data = await self.write_command(P.COMMAND_MEMORY, P.SUBCOMMAND_MEMORY_READ, payload)
        if data[0] != length or P.decodeu(data[4:8]) != address:
            raise RuntimeError(f"unexpected read response: {P.to_hex(data)}")
        return data[8:]

    async def read_controller_info(self) -> P.ControllerInfo:
        return P.ControllerInfo.from_bytes(await self.read_memory(0x40, P.ADDRESS_CONTROLLER_INFO))

    async def _read_all_calibration(self) -> None:
        self.left_calib = await self._read_stick_calibration(
            P.CALIBRATION_USER_JOYSTICK_1, P.CALIBRATION_JOYSTICK_1
        )
        if self.has_second_stick:
            self.right_calib = await self._read_stick_calibration(
                P.CALIBRATION_USER_JOYSTICK_2, P.CALIBRATION_JOYSTICK_2
            )
        if self.is_gamecube:
            try:
                cal = await self.read_memory(0x02, P.CALIBRATION_GC_TRIGGERS)
                if cal and cal[0] not in (0x00, 0xFF):
                    self.trigger_neutral = (cal[0], cal[1])
                logger.info("gc trigger neutral=%s", self.trigger_neutral)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not read GC trigger calibration: %s", exc)

    async def _read_stick_calibration(
        self, user_addr: int, factory_addr: int
    ) -> P.StickCalibration:
        data = await self.read_memory(0x0B, user_addr)
        if P.decodeu(data[:3]) == 0xFFFFFF:
            data = await self.read_memory(0x0B, factory_addr)
        return P.StickCalibration.from_bytes(data)

    # ------------------------------------------------------------------ #
    # Features / LEDs / vibration                                         #
    # ------------------------------------------------------------------ #

    async def enable_features(self, flags: int) -> None:
        payload = flags.to_bytes().ljust(4, b"\x00")
        await self.write_command(P.COMMAND_FEATURE, P.SUBCOMMAND_FEATURE_INIT, payload)
        await self.write_command(P.COMMAND_FEATURE, P.SUBCOMMAND_FEATURE_ENABLE, payload)

    async def set_player_leds(self, player: int, reversed_: bool = False) -> None:
        player = min(max(player, 1), 8)
        value = P.LED_PATTERN[player]
        if reversed_:
            value = P.reverse_bits(value, 4)
        await self.write_command(
            P.COMMAND_LEDS, P.SUBCOMMAND_LEDS_SET_PLAYER, value.to_bytes().ljust(4, b"\x00")
        )

    async def play_vibration_preset(self, preset_id: int) -> None:
        await self.write_command(
            P.COMMAND_VIBRATION,
            P.SUBCOMMAND_VIBRATION_PLAY_PRESET,
            preset_id.to_bytes().ljust(4, b"\x00"),
        )

    async def set_vibration(self, vib: P.VibrationData) -> None:
        header = (0x50 + (self._vibration_packet_id & 0x0F)).to_bytes()
        motor = header + vib.to_bytes()
        uuid = P.VIBRATION_WRITE_PRO_CONTROLLER_UUID
        # Pro/GameCube expect two motor payloads (left, right).
        await self.client.write_gatt_char(uuid, b"\x00" + motor + motor, response=False)
        self._vibration_packet_id += 1

    # ------------------------------------------------------------------ #
    # Input notifications                                                  #
    # ------------------------------------------------------------------ #

    async def start_input(self) -> None:
        def _cb(_sender, data: bytearray) -> None:
            report = P.InputReport.parse(bytes(data))
            self.battery_mv = report.battery_mv
            if self.input_callback is not None:
                self.input_callback(self, report)

        await self.client.start_notify(P.INPUT_REPORT_UUID, _cb)

    # ------------------------------------------------------------------ #
    # Type helpers                                                         #
    # ------------------------------------------------------------------ #

    @property
    def product_id(self) -> int:
        return self.info.product_id if self.info else 0

    @property
    def is_gamecube(self) -> bool:
        return self.product_id == P.NSO_GAMECUBE_PID

    @property
    def is_pro_controller(self) -> bool:
        return self.product_id == P.PRO_CONTROLLER2_PID

    @property
    def has_second_stick(self) -> bool:
        return self.product_id in (P.PRO_CONTROLLER2_PID, P.NSO_GAMECUBE_PID)
