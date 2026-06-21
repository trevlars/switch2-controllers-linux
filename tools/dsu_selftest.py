"""Local self-test for the DSU server: validates packet framing, CRC, lengths,
and IMU scaling using a loopback client (no controller needed).

Run from the repo root: python3 -m tools.dsu_selftest
"""
import socket
import struct
import sys
import time
import zlib

from ngc import protocol as P
from ngc.dsu import DSUServer, PROTOCOL_VERSION, MSG_VERSION, MSG_PORTS, MSG_DATA

PORT = 26770  # avoid clashing with a real server


def client_packet(server_id, msg_type, data=b""):
    body = struct.pack("<I", msg_type) + data
    header = b"DSUC" + struct.pack("<HH", PROTOCOL_VERSION, len(body))
    header += struct.pack("<I", 0) + struct.pack("<I", server_id)
    pkt = bytearray(header + body)
    crc = zlib.crc32(pkt) & 0xFFFFFFFF
    pkt[8:12] = struct.pack("<I", crc)
    return bytes(pkt)


def check_crc(pkt, label):
    raw = bytearray(pkt)
    got = struct.unpack_from("<I", raw, 8)[0]
    raw[8:12] = b"\x00\x00\x00\x00"
    want = zlib.crc32(raw) & 0xFFFFFFFF
    assert got == want, f"{label}: CRC mismatch {got:#x} != {want:#x}"
    length = struct.unpack_from("<H", raw, 6)[0]
    assert length == len(raw) - 16, f"{label}: bad length field {length} vs {len(raw) - 16}"


def main():
    srv = DSUServer(port=PORT)
    assert srv.start(), "server failed to start"
    try:
        # Register slot 0 as a connected controller.
        srv.set_slot(0, True, mac="11:22:33:44:55:66", battery_mv=4000)

        cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        cli.settimeout(2.0)
        addr = ("127.0.0.1", PORT)

        # 1) Version request.
        cli.sendto(client_packet(1, MSG_VERSION), addr)
        data, _ = cli.recvfrom(1024)
        check_crc(data, "version")
        assert data[:4] == b"DSUS"
        assert struct.unpack_from("<I", data, 16)[0] == MSG_VERSION
        ver = struct.unpack_from("<H", data, 20)[0]
        assert ver == PROTOCOL_VERSION, ver
        print("version OK ->", ver)

        # 2) Port info request for slots 0..3.
        req = struct.pack("<i", 4) + bytes([0, 1, 2, 3])
        cli.sendto(client_packet(1, MSG_PORTS, req), addr)
        seen = {}
        for _ in range(4):
            data, _ = cli.recvfrom(1024)
            check_crc(data, "ports")
            assert struct.unpack_from("<I", data, 16)[0] == MSG_PORTS
            slot = data[20]
            state = data[21]
            seen[slot] = state
        assert seen.get(0) == 2, f"slot0 state {seen.get(0)}"
        assert seen.get(1) == 0, f"slot1 state {seen.get(1)}"
        print("ports OK ->", seen)

        # 3) Subscribe to data (all controllers), then push a report.
        cli.sendto(client_packet(1, MSG_DATA, bytes([0, 0]) + b"\x00" * 6), addr)
        time.sleep(0.1)

        report = P.InputReport.parse(bytes(64))
        report.buttons = P.SWITCH_BUTTONS["A"] | P.SWITCH_BUTTONS["UP"]
        report.accel = (4096, -4096, 0)      # +1g, -1g, 0
        report.gyro = (164, 0, -16)          # ~+10, 0, ~-1 deg/s
        report.battery_mv = 4000
        srv.update(0, report, (128, 128, 200, 64), (255, 0))

        data, _ = cli.recvfrom(1024)
        check_crc(data, "data")
        assert struct.unpack_from("<I", data, 16)[0] == MSG_DATA
        assert len(data) == 100, f"pad data len {len(data)} != 100"
        connected = data[31]
        assert connected == 1
        ax, ay, az = struct.unpack_from("<fff", data, 56 + 20)
        gx, gy, gz = struct.unpack_from("<fff", data, 68 + 20)
        assert abs(ax - 1.0) < 0.01 and abs(ay + 1.0) < 0.01, (ax, ay, az)
        assert abs(gx - 10.0) < 0.5, (gx, gy, gz)
        print(f"data OK -> accel=({ax:.2f},{ay:.2f},{az:.2f}) gyro=({gx:.2f},{gy:.2f},{gz:.2f})")

        # Button bytes: A should set bit 0x20 in byte 17 (offset 20+17).
        d1 = data[20 + 17]
        assert d1 & 0x20, f"A button bit not set: {d1:#x}"
        d0 = data[20 + 16]
        assert d0 & 0x10, f"UP dpad bit not set: {d0:#x}"
        print("buttons OK")

        print("\nALL DSU SELF-TESTS PASSED")
        return 0
    finally:
        srv.stop()


if __name__ == "__main__":
    sys.exit(main())
