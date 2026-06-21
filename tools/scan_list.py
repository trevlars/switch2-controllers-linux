#!/usr/bin/env python3
"""List all Switch 2 controllers currently advertising (pairing mode or awake)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ngc.scanner import scan

timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0


def main() -> int:
    res = asyncio.run(scan(timeout=timeout))
    for d in res:
        print(f"{d.device.address} {d.product_id:#06x} {d.name}")
    print(f"count {len(res)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
