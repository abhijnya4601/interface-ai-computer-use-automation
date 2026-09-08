"""
Chromium launch flags shared by replay and the web console.

`--disable-dev-shm-usage` — constrained containers (Render/Koyeb/Fly free tiers, most Docker
hosts) give the container a tiny `/dev/shm`; without this flag Chromium writes there and crashes
under any real page. `--no-sandbox` — the sandbox needs root or user-namespace support that
locked-down free hosts often don't grant to a non-root process. Both are standard "Chromium in a
container" flags and harmless locally.

Set `CHROMIUM_NO_EXTRA_ARGS=1` to launch with Playwright's defaults instead (e.g. if a host
provides a proper sandbox and you'd rather keep it).
"""
from __future__ import annotations

import os

LAUNCH_ARGS: list[str] = (
    []
    if os.environ.get("CHROMIUM_NO_EXTRA_ARGS") == "1"
    else ["--disable-dev-shm-usage", "--no-sandbox"]
)
