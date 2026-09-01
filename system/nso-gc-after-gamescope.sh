#!/usr/bin/env bash
# Restart nso-gc after Game Mode starts — only when no pads are connected.
# Unconditional try-restart was killing live Pro 2 / GC sessions ~22s into Steam.
set -euo pipefail
sleep 22
STATE="${HOME}/.config/nso-gc/state.json"
connected=0
if [[ -f "$STATE" ]]; then
  connected=$(python3 - "$STATE" <<'PY' 2>/dev/null || echo 0
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print(sum(1 for c in data.get("controllers", []) if c.get("connected")))
PY
)
fi
if [[ "$connected" -eq 0 ]]; then
  systemctl --user try-restart nso-gc.service 2>/dev/null || true
fi
