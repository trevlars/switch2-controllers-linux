#!/usr/bin/env python3
"""Discover the controller's full GATT table over the raw L2CAP ATT client and
print handles + UUIDs, so we can wire characteristic handles precisely."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ngc import att

DST = sys.argv[1] if len(sys.argv) > 1 else "AA:BB:CC:DD:EE:01"
ADAPTER = sys.argv[2] if len(sys.argv) > 2 else "00:00:00:00:00:00"


def main() -> int:
    client = att.ATTClient(DST, ADAPTER, dst_type=att.LE_PUBLIC)
    print(f"connecting (raw L2CAP) to {DST} ...", flush=True)
    deadline = time.time() + 40
    while time.time() < deadline:
        if client.connect(timeout=8):
            break
    else:
        print("could not connect (keep controller in pairing mode)")
        return 1

    print(f"connected, MTU={client.mtu}\n", flush=True)
    services = client.discover_all()
    print(f"discovered {len(services)} services\n", flush=True)
    for svc in services:
        print(f"[service] {svc.uuid}  handles {svc.start:#06x}-{svc.end:#06x}")
        for ch in svc.characteristics:
            cccd = f" cccd={ch.cccd_handle:#06x}" if ch.cccd_handle else ""
            print(
                f"    [char] {ch.uuid}  val={ch.value_handle:#06x} props={ch.properties:#04x}{cccd}"
            )
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
