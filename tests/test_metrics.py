import json

from replay.metrics import _percentile, aggregate, format_table, load_traces


def _run(cap_id, status, total_ms, step_tiers):
    return {
        "kind": "replay_trace_header", "capability_id": cap_id, "status": status,
        "total_ms": total_ms, "business_outcome_code": None,
        "steps": [
            {"kind": "step", "step_id": f"s{i}", "action_type": "click", "tier": t, "ms": 10.0,
             "outcome": "ok"}
            for i, t in enumerate(step_tiers)
        ],
    }


def test_percentile_nearest_rank():
    assert _percentile([], 50) == 0.0
    assert _percentile([100.0], 95) == 100.0
    assert _percentile([10, 20, 30, 40, 50], 50) == 30
    assert _percentile([10, 20, 30, 40, 50], 95) == 50


def test_aggregate_outcome_rates_and_latency():
    runs = [
        _run("lookup_member_balance", "success", 1200, ["role_name", "role_name"]),
        _run("lookup_member_balance", "success", 1400, ["role_name", "role_name"]),
        _run("lookup_member_balance", "data_unavailable", 1600, ["role_name", "role_name"]),
        _run("lookup_member_balance", "hard_failure", 800, ["role_name", "unresolved"]),
    ]
    m = aggregate(runs)["lookup_member_balance"]
    assert m["runs"] == 4
    assert m["healthy_rate"] == 0.5           # 2 success / 4
    assert m["hard_failure_rate"] == 0.25
    assert m["data_unavailable_rate"] == 0.25
    # nearest-rank over sorted [800, 1200, 1400, 1600]
    assert m["p50_ms"] == 1400
    assert m["p95_ms"] == 1600
    assert m["outcome_rate"]["success"] == 0.5


def test_aggregate_locator_fallback_rate_is_the_drift_signal():
    runs = [
        _run("c", "success", 100, ["role_name", "role_name", "role_name", "role_name"]),
        _run("c", "success", 100, ["role_name", "structural", "text", "role_name"]),
    ]
    m = aggregate(runs)["c"]
    # 8 locator steps total, 2 on a fallback tier
    assert m["locator_fallback_rate"] == 0.25


def test_aggregate_groups_by_capability():
    runs = [
        _run("a", "success", 100, ["role_name"]),
        _run("b", "hard_failure", 100, ["unresolved"]),
    ]
    m = aggregate(runs)
    assert set(m) == {"a", "b"}
    assert m["b"]["hard_failure_rate"] == 1.0


def test_load_traces_reads_real_jsonl_and_skips_junk(tmp_path):
    good = tmp_path / "replay_run_abc_trace.jsonl"
    good.write_text(
        json.dumps({"kind": "replay_trace_header", "capability_id": "x", "status": "success",
                    "total_ms": 123}) + "\n"
        + json.dumps({"kind": "step", "step_id": "s1", "action_type": "navigate", "tier": None,
                      "ms": 50.0, "outcome": "ok"}) + "\n"
    )
    (tmp_path / "replay_run_bad_trace.jsonl").write_text("not json\n")
    (tmp_path / "unrelated.jsonl").write_text(json.dumps({"kind": "step"}) + "\n")

    runs = load_traces(tmp_path)
    assert len(runs) == 1
    assert runs[0]["capability_id"] == "x"
    assert len(runs[0]["steps"]) == 1


def test_format_table_handles_empty():
    assert format_table({}) == "no replay traces found"


def test_format_table_renders_rows():
    table = format_table(aggregate([_run("cap_x", "success", 100, ["role_name"])]))
    assert "cap_x" in table
    assert "capability" in table  # header row
