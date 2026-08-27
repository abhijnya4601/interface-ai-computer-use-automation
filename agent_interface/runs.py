"""
Run registry — an append-only JSONL log of every discovery and replay run, so the API and the
dashboard have one queryable history. Deliberately a flat file, not a DB: the assignment
penalises scaling infrastructure, and a demo's run count is tiny.

Each line is one run: id, kind (discovery|replay), capability, params (redacted), status,
outputs (redacted), failure_detail, timings, tier log, evidence refs.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from guardrails.policy import redact

REGISTRY_PATH = Path(__file__).parent.parent / "evidence" / "runs.jsonl"


def record_run(
    run_id: str,
    kind: str,
    capability_id: str,
    *,
    status: str,
    params: dict | None = None,
    outputs: dict | None = None,
    business_outcome_code: str | None = None,
    failure_detail: dict | None = None,
    started_at: float | None = None,
    tier_log: list | None = None,
    evidence_refs: list[str] | None = None,
    recovery: list | None = None,
    extra: dict | None = None,
) -> dict:
    now = time.time()
    entry = {
        "run_id": run_id,
        "kind": kind,
        "capability_id": capability_id,
        "status": status,
        "params": redact(params or {}),
        "outputs": redact(outputs or {}),
        "business_outcome_code": business_outcome_code,
        "failure_detail": failure_detail,
        "started_at": started_at or now,
        "finished_at": now,
        "duration_s": round(now - started_at, 2) if started_at else None,
        "tier_log": tier_log or [],
        "evidence_refs": evidence_refs or [],
        "recovery": recovery or None,
    }
    if extra:
        entry.update(extra)
    REGISTRY_PATH.parent.mkdir(exist_ok=True)
    with open(REGISTRY_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def list_runs(limit: int | None = None) -> list[dict]:
    if not REGISTRY_PATH.exists():
        return []
    rows = []
    for line in REGISTRY_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows.reverse()  # newest first
    return rows[:limit] if limit else rows


def get_run(run_id: str) -> dict | None:
    for row in list_runs():
        if row.get("run_id") == run_id:
            return row
    return None
