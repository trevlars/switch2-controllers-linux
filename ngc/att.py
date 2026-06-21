"""Minimal GATT/ATT client over a raw L2CAP socket.

This bypasses BlueZ's GATT layer entirely and connects with BT_SECURITY_LOW,
which is required by Switch 2 controllers (they drop any link that attempts SMP
pairing). A background reader thread dispatches Handle-Value-Notifications to a
callback for the lowest possible input latency.

Prerequisite: BlueZ must not be actively scanning the adapter while we initiate
the connection (most controllers can't scan and initiate simultaneously).
"""

from __future__ import annotations

import ctypes
import os
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

AF_BLUETOOTH = 31
BTPROTO_L2CAP = 0
SOL_BLUETOOTH = 274
BT_SECURITY = 4
BT_SECURITY_LOW = 1
ATT_CID = 4

LE_PUBLIC = 1
LE_RANDOM = 2

# ATT opcodes
ATT_ERROR_RSP = 0x01
ATT_EXCHANGE_MTU_REQ = 0x02
ATT_EXCHANGE_MTU_RSP = 0x03
ATT_FIND_INFO_REQ = 0x04
ATT_FIND_INFO_RSP = 0x05
ATT_READ_BY_TYPE_REQ = 0x08
ATT_READ_BY_TYPE_RSP = 0x09
ATT_READ_REQ = 0x0A
ATT_READ_RSP = 0x0B
ATT_READ_BY_GROUP_REQ = 0x10
ATT_READ_BY_GROUP_RSP = 0x11
ATT_WRITE_REQ = 0x12
ATT_WRITE_RSP = 0x13
ATT_HANDLE_VALUE_NTF = 0x1B
ATT_WRITE_CMD = 0x52

# GATT attribute type UUIDs (16-bit)
UUID_PRIMARY_SERVICE = 0x2800
UUID_CHARACTERISTIC = 0x2803
UUID_CCCD = 0x2902

_libc = ctypes.CDLL("libc.so.6", use_errno=True)


def baddr(mac: str) -> bytes:
    return bytes(int(x, 16) for x in reversed(mac.split(":")))


def _sockaddr_l2(psm: int, bdaddr: bytes, cid: int, atype: int) -> bytes:
    return struct.pack("<HH6sHB", AF_BLUETOOTH, psm, bdaddr, cid, atype) + b"\x00"


def uuid_to_str(raw: bytes) -> str:
    if len(raw) == 2:
        return f"{struct.unpack('<H', raw)[0]:04x}"
    if len(raw) == 16:
        b = raw[::-1]
        return (
            f"{b[0:4].hex()}-{b[4:6].hex()}-{b[6:8].hex()}-{b[8:10].hex()}-{b[10:16].hex()}"
        )
    return raw.hex()


class ATTError(Exception):
    def __init__(self, req_opcode: int, handle: int, code: int):
        self.req_opcode = req_opcode
        self.handle = handle
        self.code = code
        super().__init__(f"ATT error op={req_opcode:#04x} handle={handle:#06x} code={code:#04x}")


@dataclass
class Characteristic:
    decl_handle: int
    properties: int
    value_handle: int
    uuid: str
    cccd_handle: Optional[int] = None


@dataclass
class Service:
    start: int
    end: int
    uuid: str
    characteristics: list[Characteristic] = field(default_factory=list)


class ATTClient:
    def __init__(self, dst: str, adapter: str, dst_type: int = LE_PUBLIC):
        self.dst = dst
        self.adapter = adapter
        self.dst_type = dst_type
        self.sock: Optional[socket.socket] = None
        self.mtu = 23

        self._reader: Optional[threading.Thread] = None
        self._running = False
        self._resp_lock = threading.Lock()
        self._resp_event = threading.Event()
        self._resp_pdu: Optional[bytes] = None
        self.notification_cb: Optional[Callable[[int, bytes], None]] = None
        self.disconnect_cb: Optional[Callable[[], None]] = None
        self._closing = False
        self.last_packet_at: float = 0.0

    # ------------------------------------------------------------------ #
    # Connection                                                          #
    # ------------------------------------------------------------------ #

    def connect(self, timeout: float = 10.0) -> tuple[bool, str]:
        """Try LE public and random target address types (bonded pads vary)."""
        per = max(0.1, timeout / 2)
        errors: list[str] = []
        for dst_type in (LE_PUBLIC, LE_RANDOM):
            ok, detail = self._connect_once(dst_type, per)
            if ok:
                self.dst_type = dst_type
                return True, "ok"
            label = "public" if dst_type == LE_PUBLIC else "random"
            errors.append(f"{label}: {detail}")
        return False, "; ".join(errors)

    def _connect_once(self, dst_type: int, timeout: float) -> tuple[bool, str]:
        s = socket.socket(AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
        s.setsockopt(SOL_BLUETOOTH, BT_SECURITY, struct.pack("BB", BT_SECURITY_LOW, 0))
        fd = s.fileno()

        bind_addr = _sockaddr_l2(0, baddr(self.adapter), ATT_CID, LE_PUBLIC)
        if _libc.bind(fd, bind_addr, len(bind_addr)) != 0:
            s.close()
            return False, f"L2CAP bind failed (errno {ctypes.get_errno()})"

        s.setblocking(False)
        conn_addr = _sockaddr_l2(0, baddr(self.dst), ATT_CID, dst_type)
        r = _libc.connect(fd, conn_addr, len(conn_addr))
        errno = ctypes.get_errno()
        if r != 0 and errno not in (115, 114):  # EINPROGRESS / EALREADY
            s.close()
            return False, f"connect errno {errno}"

        _, w, _ = select.select([], [fd], [], timeout)
        if not w:
            s.close()
            return False, "timeout (adapter may still be scanning)"

        soerr = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if soerr != 0:
            s.close()
            return False, f"SO_ERROR {soerr}"

        s.setblocking(True)
        self._closing = False
        self.sock = s
        self.last_packet_at = time.monotonic()
        self._start_reader()
        try:
            self.exchange_mtu(247)
        except Exception:
            pass
        return True, "ok"

    def close(self) -> None:
        self._closing = True
        self._running = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    @property
    def is_connected(self) -> bool:
        return self.sock is not None and self._running

    # ------------------------------------------------------------------ #
    # Reader thread                                                       #
    # ------------------------------------------------------------------ #

    def _start_reader(self) -> None:
        self._running = True
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()

    def _reader_loop(self) -> None:
        sock = self.sock
        while self._running and sock is not None:
            try:
                pdu = sock.recv(512)
            except OSError:
                break
            if not pdu:
                break
            self.last_packet_at = time.monotonic()
            opcode = pdu[0]
            if opcode == ATT_HANDLE_VALUE_NTF:
                handle = struct.unpack_from("<H", pdu, 1)[0]
                if self.notification_cb is not None:
                    try:
                        self.notification_cb(handle, pdu[3:])
                    except Exception:
                        pass
            else:
                self._resp_pdu = pdu
                self._resp_event.set()
        self._running = False
        if not self._closing and self.disconnect_cb is not None:
            try:
                self.disconnect_cb()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Request/response                                                    #
    # ------------------------------------------------------------------ #

    def _request(self, pdu: bytes, timeout: float = 3.0) -> bytes:
        with self._resp_lock:
            self._resp_event.clear()
            self._resp_pdu = None
            self.sock.send(pdu)
            if not self._resp_event.wait(timeout):
                raise TimeoutError("ATT request timed out")
            resp = self._resp_pdu
        if resp and resp[0] == ATT_ERROR_RSP:
            _, req_op, handle, code = struct.unpack_from("<BBHB", resp, 0)
            raise ATTError(req_op, handle, code)
        return resp

    def exchange_mtu(self, client_mtu: int = 247) -> int:
        resp = self._request(struct.pack("<BH", ATT_EXCHANGE_MTU_REQ, client_mtu))
        if resp[0] == ATT_EXCHANGE_MTU_RSP:
            server_mtu = struct.unpack_from("<H", resp, 1)[0]
            self.mtu = min(client_mtu, server_mtu)
        return self.mtu

    def read(self, handle: int) -> bytes:
        resp = self._request(struct.pack("<BH", ATT_READ_REQ, handle))
        return resp[1:]

    def write_request(self, handle: int, value: bytes) -> None:
        self._request(struct.pack("<BH", ATT_WRITE_REQ, handle) + value)

    def write_command(self, handle: int, value: bytes) -> None:
        self.sock.send(struct.pack("<BH", ATT_WRITE_CMD, handle) + value)

    def subscribe(self, cccd_handle: int, notifications: bool = True) -> None:
        value = struct.pack("<H", 0x0001 if notifications else 0x0000)
        self.write_request(cccd_handle, value)

    # ------------------------------------------------------------------ #
    # Discovery                                                           #
    # ------------------------------------------------------------------ #

    def discover_services(self) -> list[Service]:
        services: list[Service] = []
        start = 0x0001
        while start <= 0xFFFF:
            req = struct.pack("<BHHH", ATT_READ_BY_GROUP_REQ, start, 0xFFFF, UUID_PRIMARY_SERVICE)
            try:
                resp = self._request(req)
            except ATTError as exc:
                if exc.code == 0x0A:  # attribute not found -> done
                    break
                raise
            if resp[0] != ATT_READ_BY_GROUP_RSP:
                break
            length = resp[1]
            data = resp[2:]
            last_end = 0
            for i in range(0, len(data), length):
                entry = data[i : i + length]
                s_handle, e_handle = struct.unpack_from("<HH", entry, 0)
                uuid = uuid_to_str(entry[4:length])
                services.append(Service(s_handle, e_handle, uuid))
                last_end = e_handle
            if last_end >= 0xFFFF or last_end == 0:
                break
            start = last_end + 1
        return services

    def discover_characteristics(self, service: Service) -> None:
        start = service.start
        while start <= service.end:
            req = struct.pack("<BHHH", ATT_READ_BY_TYPE_REQ, start, service.end, UUID_CHARACTERISTIC)
            try:
                resp = self._request(req)
            except ATTError as exc:
                if exc.code == 0x0A:
                    break
                raise
            if resp[0] != ATT_READ_BY_TYPE_RSP:
                break
            length = resp[1]
            data = resp[2:]
            last_handle = 0
            for i in range(0, len(data), length):
                entry = data[i : i + length]
                decl_handle = struct.unpack_from("<H", entry, 0)[0]
                properties = entry[2]
                value_handle = struct.unpack_from("<H", entry, 3)[0]
                uuid = uuid_to_str(entry[5:length])
                service.characteristics.append(
                    Characteristic(decl_handle, properties, value_handle, uuid)
                )
                last_handle = decl_handle
            if last_handle == 0:
                break
            start = last_handle + 1

    def discover_cccds(self, service: Service) -> None:
        chars = service.characteristics
        for idx, ch in enumerate(chars):
            scan_start = ch.value_handle + 1
            scan_end = chars[idx + 1].decl_handle - 1 if idx + 1 < len(chars) else service.end
            if scan_start > scan_end:
                continue
            handle = scan_start
            while handle <= scan_end:
                req = struct.pack("<BHH", ATT_FIND_INFO_REQ, handle, scan_end)
                try:
                    resp = self._request(req)
                except ATTError:
                    break
                if resp[0] != ATT_FIND_INFO_RSP:
                    break
                fmt = resp[1]
                entry_len = 4 if fmt == 1 else 18
                data = resp[2:]
                last = 0
                for i in range(0, len(data), entry_len):
                    entry = data[i : i + entry_len]
                    h = struct.unpack_from("<H", entry, 0)[0]
                    uuid = uuid_to_str(entry[2:entry_len])
                    if uuid == "2902":
                        ch.cccd_handle = h
                    last = h
                if last == 0 or last >= scan_end:
                    break
                handle = last + 1

    def discover_all(self) -> list[Service]:
        services = self.discover_services()
        for svc in services:
            self.discover_characteristics(svc)
            self.discover_cccds(svc)
        return services
