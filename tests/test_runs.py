"""
agent_interface/runs.py - the append-only run registry behind the console's History view.
"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _fresh_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_RUNS_PATH", str(tmp_path / "runs.jsonl"))
    import agent_interface.runs as runs
    importlib.reload(runs)
    return runs


def test_record_then_list_is_newest_first(tmp_path, monkeypatch):
    runs = _fresh_registry(tmp_path, monkeypatch)
    runs.record_run("r1", "replay", "lookup_member_balance", status="success", started_at=1.0)
    runs.record_run("r2", "ask", "open_subaccount", status="business_outcome",
                    business_outcome_code="OPERATOR_DECLINED", started_at=2.0, escalations=1)
    rows = runs.list_runs()
    assert [r["run_id"] for r in rows] == ["r2", "r1"]
    assert rows[0]["kind"] == "ask"
    assert rows[0]["escalations"] == 1
    assert rows[0]["duration_s"] is not None


def test_params_and_outputs_are_redacted(tmp_path, monkeypatch):
    runs = _fresh_registry(tmp_path, monkeypatch)
    runs.record_run("r1", "replay", "lookup", status="success",
                    params={"member_id": "12345"},
                    outputs={"email": "jane.doe@example.com"}, started_at=1.0)
    row = runs.list_runs()[0]
    assert "jane.doe@example.com" not in str(row["outputs"])


def test_get_run_and_limit(tmp_path, monkeypatch):
    runs = _fresh_registry(tmp_path, monkeypatch)
    for i in range(5):
        runs.record_run(f"r{i}", "replay", "c", status="success", started_at=float(i))
    assert runs.get_run("r3")["run_id"] == "r3"
    assert runs.get_run("nope") is None
    assert len(runs.list_runs(limit=2)) == 2


def test_list_runs_is_empty_when_no_file(tmp_path, monkeypatch):
    runs = _fresh_registry(tmp_path, monkeypatch)
    assert runs.list_runs() == []
