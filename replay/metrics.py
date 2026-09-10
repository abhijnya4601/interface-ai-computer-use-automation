"""
Aggregate a directory of `replay_<run_id>_trace.jsonl` files (written by replay/engine.py) into
per-capability operational metrics - the numbers a team actually watches once capabilities are
running in production:

  - outcome mix: success / business_outcome / data_unavailable / recoverable / hard_failure rate
  - latency: p50 / p95 total run time
  - drift: fraction of resolved steps that needed a fallback tier (structural / text /
    table_position / unresolved) instead of tier-1 role_name. REPORT §3 calls the tier log a
    "free drift-detection signal"; this is that signal turned into a number you can alert on.

No new infrastructure: it's a pure function over files that replay already writes.

    python -m replay.metrics                 # aggregate ./evidence
    python -m replay.metrics path/to/dir
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_FALLBACK_TIERS = {"structural", "text", "text (fallback)", "table_position", "unresolved"}
_TERMINAL_OK = {"success", "business_outcome", "recoverable_handled"}


def load_traces(evidence_dir: str | Path) -> list[dict]:
    """Return one dict per run: the header fields plus `steps: list[dict]`. Skips files that
    aren't valid replay traces rather than raising."""
    runs: list[dict] = []
    for path in sorted(Path(evidence_dir).glob("replay_*_trace.jsonl")):
        try:
            lines = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
        except (OSError, json.JSONDecodeError):
            continue
        if not lines or lines[0].get("kind") != "replay_trace_header":
            continue
        header = dict(lines[0])
        header["steps"] = [x for x in lines[1:] if x.get("kind") == "step"]
        runs.append(header)
    return runs


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    # nearest-rank
    k = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return round(ordered[k], 1)


def aggregate(runs: list[dict]) -> dict:
    """Group runs by capability_id and compute the metrics dict for each."""
    by_cap: dict[str, list[dict]] = {}
    for run in runs:
        by_cap.setdefault(run.get("capability_id", "?"), []).append(run)

    out: dict[str, dict] = {}
    for cap_id, cap_runs in sorted(by_cap.items()):
        n = len(cap_runs)
        statuses: dict[str, int] = {}
        for r in cap_runs:
            statuses[r.get("status", "?")] = statuses.get(r.get("status", "?"), 0) + 1
        totals = [float(r.get("total_ms", 0.0)) for r in cap_runs]

        resolved_steps = [
            s for r in cap_runs for s in r["steps"]
            if s.get("tier") not in (None, "role_name")
            and s.get("action_type") in ("click", "type", "select", "extract")
        ]
        all_locator_steps = [
            s for r in cap_runs for s in r["steps"]
            if s.get("action_type") in ("click", "type", "select", "extract")
        ]
        fallback_steps = [s for s in resolved_steps if s.get("tier") in _FALLBACK_TIERS]

        out[cap_id] = {
            "runs": n,
            "outcome_rate": {k: round(v / n, 3) for k, v in sorted(statuses.items())},
            "healthy_rate": round(
                sum(v for k, v in statuses.items() if k in _TERMINAL_OK) / n, 3
            ),
            "hard_failure_rate": round(statuses.get("hard_failure", 0) / n, 3),
            "data_unavailable_rate": round(statuses.get("data_unavailable", 0) / n, 3),
            "p50_ms": _percentile(totals, 50),
            "p95_ms": _percentile(totals, 95),
            "locator_fallback_rate": (
                round(len(fallback_steps) / len(all_locator_steps), 3) if all_locator_steps else 0.0
            ),
        }
    return out


def format_table(metrics: dict) -> str:
    if not metrics:
        return "no replay traces found"
    cols = ["capability", "runs", "healthy", "hard_fail", "data_unavail", "p50_ms", "p95_ms", "fallback"]
    rows = [cols]
    for cap_id, m in metrics.items():
        rows.append([
            cap_id, str(m["runs"]), f"{m['healthy_rate']:.0%}",
            f"{m['hard_failure_rate']:.0%}", f"{m['data_unavailable_rate']:.0%}",
            f"{m['p50_ms']:.0f}", f"{m['p95_ms']:.0f}", f"{m['locator_fallback_rate']:.0%}",
        ])
    widths = [max(len(r[i]) for r in rows) for i in range(len(cols))]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)) for r in rows)


def main(argv: list[str]) -> int:
    evidence_dir = argv[1] if len(argv) > 1 else str(Path(__file__).parent.parent / "evidence")
    runs = load_traces(evidence_dir)
    print(f"loaded {len(runs)} replay traces from {evidence_dir}\n")
    print(format_table(aggregate(runs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
