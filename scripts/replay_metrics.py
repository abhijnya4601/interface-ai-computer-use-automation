"""
Print per-capability replay metrics from the trace files replay/engine.py writes into
evidence/ (outcome mix, p50/p95 latency, locator-fallback rate as the drift signal).

    python scripts/replay_metrics.py [evidence_dir]

Thin wrapper over `python -m replay.metrics` so it sits with the other scripts/.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from replay.metrics import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
