"""DSU (cemuhook) UDP server exposing controller motion to emulators.

Implements the cemuhook UDP protocol (version 1001) on 127.0.0.1:26760 with up
to four slots. Each connected :class:`~ngc.device.SwitchController` feeds its
slot the latest button / stick / IMU state; Dolphin, Cemu and Ryujinx connect as
clients and read accelerometer + gyroscope data for motion controls.

Protocol reference: https://v1993.github.io/cemuhook-protocol/
"""

from __future__ import annotations

import logging
import random
import socket
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Optional

from . import protocol as P

logger = logging.getLogger(__name__)

DSU_HOST = "127.0.0.1"
DSU_PORT = 26760
PROTOCOL_VERSION = 1001

# Message types (shared between incoming requests and outgoing responses).
MSG_VERSION = 0x100000
MSG_PORTS = 0x100001
MSG_DATA = 0x100002

# Slot state / model / connection constants.
STATE_DISCONNECTED = 0
STATE_CONNECTED = 2
MODEL_FULL_GYRO = 2
CONN_BLUETOOTH = 2

# IMU scaling. The Switch IMU (LSM6DS3) reports signed 16-bit samples; the
# defaults below match the common Switch full-scale (±8 g, ±2000 deg/s). Axis
# signs are split out so orientation can be flipped without touching the packing.
ACCEL_G_PER_LSB = 1.0 / 4096.0
GYRO_DPS_PER_LSB = 1.0 / 16.4

# Map raw (x, y, z) IMU axes -> DSU (accel x/y/z, gyro pitch/yaw/roll).
# Tweak signs here if motion feels inverted in an emulator.
ACCEL_SIGN = (1.0, 1.0, 1.0)
GYRO_SIGN = (1.0, 1.0, 1.0)

CLIENT_TIMEOUT = 5.0  # drop clients that stop requesting data


def _dsu_battery(battery_mv: int) -> int:
    """Map a millivolt reading to a DSU battery enum (best effort)."""
    if battery_mv <= 0:
        return 0x00
    if battery_mv >= 4000:
        return 0x05  # full
    if battery_mv >= 3800:
        return 0x04  # high
    if battery_mv >= 3600:
        return 0x03  # medium
    if battery_mv >= 3400:
        return 0x02  # low
    return 0x01      # dying


@dataclass
class _Slot:
    connected: bool = False
    mac: bytes = b"\x00" * 6
    battery: int = 0x00
    packet_number: int = 0
    # Latest input snapshot (filled by update()).
    buttons: int = 0
    sticks: tuple = (128, 128, 128, 128)  # lx, ly, rx, ry (0..255, 128 neutral)
    triggers: tuple = (0, 0)              # lt, rt (0..255)
    accel: tuple = (0.0, 0.0, 0.0)        # g
    gyro: tuple = (0.0, 0.0, 0.0)         # deg/s
    motion_ts: int = 0                    # microseconds
    lock: threading.Lock = field(default_factory=threading.Lock)


def _mac_str_to_bytes(mac: str) -> bytes:
    try:
        return bytes(int(b, 16) for b in mac.split(":"))[:6].ljust(6, b"\x00")
    except Exception:  # noqa: BLE001
        return b"\x00" * 6


class DSUServer:
    """Threaded DSU/cemuhook server. Start with :meth:`start`, register a
    controller per slot with :meth:`set_slot`, and push input with
    :meth:`update`."""

    def __init__(self, host: str = DSU_HOST, port: int = DSU_PORT):
        self.host = host
        self.port = port
        self.server_id = random.getrandbits(32)
        self.slots = [_Slot() for _ in range(4)]
        self.sock: Optional[socket.socket] = None
        self._clients: dict = {}  # addr -> last_seen monotonic
        self._clients_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------- #

    def start(self) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
        except OSError as exc:
            logger.warning("DSU server bind failed on %s:%d (%s); motion disabled",
                           self.host, self.port, exc)
            return False
        sock.settimeout(0.5)
        self.sock = sock
        self._thread = threading.Thread(target=self._recv_loop, name="dsu", daemon=True)
        self._thread.start()
        logger.info("DSU server listening on %s:%d", self.host, self.port)
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # -- slot registration -------------------------------------------- #

    def set_slot(self, slot: int, connected: bool, mac: str = "", battery_mv: int = 0) -> None:
        if not 0 <= slot < 4:
            return
        s = self.slots[slot]
        with s.lock:
            s.connected = connected
            s.mac = _mac_str_to_bytes(mac) if mac else b"\x00" * 6
            s.battery = _dsu_battery(battery_mv)
            if not connected:
                s.accel = (0.0, 0.0, 0.0)
                s.gyro = (0.0, 0.0, 0.0)

    def update(self, slot: int, report: "P.InputReport",
               sticks: tuple, triggers: tuple) -> None:
        """Push the latest input + IMU snapshot for a slot and broadcast it to
        any subscribed clients. ``sticks`` is (lx, ly, rx, ry) in 0..255 with 128
        neutral; ``triggers`` is (lt, rt) in 0..255."""
        if not 0 <= slot < 4:
            return
        s = self.slots[slot]
        ax, ay, az = report.accel
        gx, gy, gz = report.gyro
        with s.lock:
            s.buttons = report.buttons
            s.sticks = sticks
            s.triggers = triggers
            s.accel = (
                ax * ACCEL_G_PER_LSB * ACCEL_SIGN[0],
                ay * ACCEL_G_PER_LSB * ACCEL_SIGN[1],
                az * ACCEL_G_PER_LSB * ACCEL_SIGN[2],
            )
            s.gyro = (
                gx * GYRO_DPS_PER_LSB * GYRO_SIGN[0],
                gy * GYRO_DPS_PER_LSB * GYRO_SIGN[1],
                gz * GYRO_DPS_PER_LSB * GYRO_SIGN[2],
            )
            s.motion_ts = time.monotonic_ns() // 1000
            s.battery = _dsu_battery(report.battery_mv)
        self._broadcast(slot)

    # -- packet helpers ----------------------------------------------- #

    def _finish(self, message_type: int, data: bytes) -> bytes:
        body = struct.pack("<I", message_type) + data
        header = b"DSUS" + struct.pack("<HH", PROTOCOL_VERSION, len(body))
        header += struct.pack("<I", 0) + struct.pack("<I", self.server_id)
        packet = bytearray(header + body)
        crc = zlib.crc32(packet) & 0xFFFFFFFF
        packet[8:12] = struct.pack("<I", crc)
        return bytes(packet)

    def _shared_header(self, slot: int) -> bytes:
        s = self.slots[slot]
        state = STATE_CONNECTED if s.connected else STATE_DISCONNECTED
        model = MODEL_FULL_GYRO if s.connected else 0
        conn = CONN_BLUETOOTH if s.connected else 0
        return struct.pack("<BBBB", slot, state, model, conn) + s.mac + struct.pack("<B", s.battery)

    def _port_info(self, slot: int) -> bytes:
        return self._finish(MSG_PORTS, self._shared_header(slot) + b"\x00")

    def _pad_data(self, slot: int) -> bytes:
        s = self.slots[slot]
        with s.lock:
            connected = s.connected
            buttons = s.buttons
            lx, ly, rx, ry = s.sticks
            lt, rt = s.triggers
            accel = s.accel
            gyro = s.gyro
            motion_ts = s.motion_ts
            s.packet_number = (s.packet_number + 1) & 0xFFFFFFFF
            pkt_num = s.packet_number

        data = bytearray(self._shared_header(slot))
        data += struct.pack("<B", 1 if connected else 0)
        data += struct.pack("<I", pkt_num)

        # Button bitmasks (DSU layout). Most setups use DSU for motion only, but
        # we fill these so the device is fully usable as a motion+button source.
        b = buttons
        m = P.SWITCH_BUTTONS
        d0 = 0
        if b & m["LEFT"]:  d0 |= 0x80
        if b & m["DOWN"]:  d0 |= 0x40
        if b & m["RIGHT"]: d0 |= 0x20
        if b & m["UP"]:    d0 |= 0x10
        if b & m["PLUS"]:  d0 |= 0x08
        if b & m["R_STK"]: d0 |= 0x04
        if b & m["L_STK"]: d0 |= 0x02
        if b & m["MINUS"]: d0 |= 0x01
        d1 = 0
        if b & m["Y"]:  d1 |= 0x80
        if b & m["B"]:  d1 |= 0x40
        if b & m["A"]:  d1 |= 0x20
        if b & m["X"]:  d1 |= 0x10
        if b & m["R"]:  d1 |= 0x08
        if b & m["L"]:  d1 |= 0x04
        if b & m["ZR"]: d1 |= 0x02
        if b & m["ZL"]: d1 |= 0x01
        data += struct.pack("<BB", d0, d1)
        data += struct.pack("<B", 1 if b & m["HOME"] else 0)
        data += struct.pack("<B", 0)  # touch button

        data += struct.pack("<BBBB", lx & 0xFF, ly & 0xFF, rx & 0xFF, ry & 0xFF)
        # Analog dpad
        data += struct.pack("<BBBB",
                            255 if b & m["LEFT"] else 0,
                            255 if b & m["DOWN"] else 0,
                            255 if b & m["RIGHT"] else 0,
                            255 if b & m["UP"] else 0)
        # Analog face buttons
        data += struct.pack("<BBBB",
                            255 if b & m["Y"] else 0,
                            255 if b & m["B"] else 0,
                            255 if b & m["A"] else 0,
                            255 if b & m["X"] else 0)
        # Analog R1/L1/R2/L2
        data += struct.pack("<BBBB",
                            255 if b & m["R"] else 0,
                            255 if b & m["L"] else 0,
                            rt & 0xFF,
                            lt & 0xFF)
        # Two touch points (inactive)
        data += b"\x00" * 12
        # Motion timestamp + IMU
        data += struct.pack("<Q", motion_ts)
        data += struct.pack("<fff", *accel)
        data += struct.pack("<fff", *gyro)
        return self._finish(MSG_DATA, bytes(data))

    # -- networking --------------------------------------------------- #

    def _broadcast(self, slot: int) -> None:
        if self.sock is None:
            return
        now = time.monotonic()
        with self._clients_lock:
            stale = [a for a, t in self._clients.items() if now - t > CLIENT_TIMEOUT]
            for a in stale:
                del self._clients[a]
            clients = list(self._clients)
        if not clients:
            return
        packet = self._pad_data(slot)
        for addr in clients:
            try:
                self.sock.sendto(packet, addr)
            except OSError:
                pass

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 20 or data[:4] != b"DSUC":
                continue
            msg_type = struct.unpack_from("<I", data, 16)[0]
            payload = data[20:]
            try:
                if msg_type == MSG_VERSION:
                    self.sock.sendto(self._finish(MSG_VERSION, struct.pack("<H", PROTOCOL_VERSION)), addr)
                elif msg_type == MSG_PORTS:
                    self._handle_ports(payload, addr)
                elif msg_type == MSG_DATA:
                    with self._clients_lock:
                        self._clients[addr] = time.monotonic()
            except OSError:
                pass

    def _handle_ports(self, payload: bytes, addr) -> None:
        if len(payload) < 4:
            return
        count = struct.unpack_from("<i", payload, 0)[0]
        count = max(0, min(count, 4))
        for i in range(count):
            if 4 + i >= len(payload):
                break
            slot = payload[4 + i]
            if 0 <= slot < 4:
                self.sock.sendto(self._port_info(slot), addr)
