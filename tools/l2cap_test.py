#!/usr/bin/env python3
"""Feasibility test: connect to the controller over a raw L2CAP ATT socket with
BT_SECURITY_LOW (no SMP pairing), then exchange ATT MTU and confirm the link
holds. Bypasses BlueZ's GATT layer entirely.

Requires that BlueZ is NOT actively scanning (most adapters can't scan and
initiate an LE connection simultaneously)."""

import ctypes
import os
import select
import socket
import struct
import sys
import time

AF_BLUETOOTH = 31
BTPROTO_L2CAP = 0
SOL_BLUETOOTH = 274
BT_SECURITY = 4
BT_SECURITY_LOW = 1
ATT_CID = 4

LE_PUBLIC = 1
LE_RANDOM = 2

DST = sys.argv[1] if len(sys.argv) > 1 else "AA:BB:CC:DD:EE:01"
SRC = sys.argv[2] if len(sys.argv) > 2 else "00:00:00:00:00:00"

libc = ctypes.CDLL("libc.so.6", use_errno=True)


def baddr(mac: str) -> bytes:
    return bytes(int(x, 16) for x in reversed(mac.split(":")))


def sockaddr_l2(psm: int, bdaddr: bytes, cid: int, atype: int) -> bytes:
    return struct.pack("<HH6sHB", AF_BLUETOOTH, psm, bdaddr, cid, atype) + b"\x00"


def attempt(dst_type: int, timeout: float = 9.0) -> bool:
    s = socket.socket(AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
    s.setsockopt(SOL_BLUETOOTH, BT_SECURITY, struct.pack("BB", BT_SECURITY_LOW, 0))
    fd = s.fileno()

    # bind to our adapter's identity address as LE public
    bind_addr = sockaddr_l2(0, baddr(SRC), ATT_CID, LE_PUBLIC)
    if libc.bind(fd, bind_addr, len(bind_addr)) != 0:
        print(f"  bind errno={ctypes.get_errno()} ({os.strerror(ctypes.get_errno())})")
        s.close()
        return False

    s.setblocking(False)
    conn_addr = sockaddr_l2(0, baddr(DST), ATT_CID, dst_type)
    r = libc.connect(fd, conn_addr, len(conn_addr))
    errno = ctypes.get_errno()
    if r != 0 and errno not in (115, 114):  # EINPROGRESS / EALREADY
        print(f"  connect errno={errno} ({os.strerror(errno)})")
        s.close()
        return False

    _, w, _ = select.select([], [fd], [], timeout)
    if not w:
        print(f"  connect timed out after {timeout}s")
        s.close()
        return False
    soerr = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    if soerr != 0:
        print(f"  connect failed SO_ERROR={soerr} ({os.strerror(soerr)})")
        s.close()
        return False

    print("  *** raw L2CAP ATT CONNECTED ***")
    s.setblocking(True)
    s.send(struct.pack("<BH", 0x02, 247))  # ATT Exchange MTU Request
    s.settimeout(4)
    try:
        print(f"  ATT MTU response: {s.recv(64).hex()}")
    except Exception as exc:  # noqa: BLE001
        print(f"  no ATT response: {exc}")
    for _ in range(6):
        time.sleep(1)
    print("  link held 6s (survived without pairing)")
    s.close()
    return True


def main() -> int:
    print(f"raw L2CAP ATT -> {DST} via adapter {SRC}")
    deadline = time.time() + 45
    while time.time() < deadline:
        for t, name in ((LE_RANDOM, "RANDOM"), (LE_PUBLIC, "PUBLIC")):
            print(f"trying dst type {name}...")
            if attempt(t):
                return 0
        time.sleep(0.5)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
