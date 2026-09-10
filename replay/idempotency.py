"""
File-backed idempotency ledger for risky (state-mutating) replays.

A back-office system that retries a failed job must not double-open a sub-account or double-file
a dispute. The caller passes a stable `idempotency_key` for the business operation; the first
completed risky replay under that key is recorded here, and any later replay with the same key
returns that recorded outcome instead of touching the target again.

Safe capabilities are read-only, so they're never ledgered - replaying a balance lookup twice
is fine and shouldn't be blocked.

Deliberately a JSONL file, not a database: same "no premature infrastructure" call as the rest
of the build. A real deployment swaps this module for a row in Postgres with a unique
constraint; the interface (`lookup` / `record`) stays the same.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from common.logging import get_logger

_LEDGER = Path(__file__).parent.parent / "evidence" / "replay_ledger.jsonl"
_log = get_logger("idempotency")


def lookup(key: str) -> dict | None:
    """Return the recorded entry `{key, ts, capability_id, result}` for `key`, or None."""
    if not key or not _LEDGER.exists():
        return None
    try:
        for line in _LEDGER.read_text().splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("key") == key:
                return entry
    except (OSError, json.JSONDecodeError):
        return None
    return None


def record(key: str, capability_id: str, result_dump: dict) -> None:
    """Append a completed risky replay. Best-effort: a ledger write failing must not fail the
    replay that already succeeded (but it does mean a retry could re-execute - logged loudly)."""
    if not key:
        return
    try:
        _LEDGER.parent.mkdir(exist_ok=True)
        with _LEDGER.open("a") as f:
            f.write(json.dumps({
                "key": key, "ts": time.time(), "capability_id": capability_id,
                "result": result_dump,
            }, default=str) + "\n")
    except Exception as exc:  # best-effort by design: never fail a completed replay
        _log.warning("ledger_write_failed", key=key, error=str(exc))
