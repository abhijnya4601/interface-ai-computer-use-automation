"""
Offline tests for the run registry (agent_interface/runs.py) and the capability API
(api/app.py) via Flask's test client, with the invoke path monkeypatched so no browser or
network is touched. The live end-to-end (real MERIDIAN invoke, real escalation approve/decline)
is in DECISIONS.md D41 / evidence/runs.jsonl.
"""
import json

import pytest

import agent_interface.runs as runs_mod
from artifact.schema import Result


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "runs.jsonl"
    monkeypatch.setattr(runs_mod, "REGISTRY_PATH", path)
    return path


def test_record_and_list_runs_newest_first(registry):
    runs_mod.record_run("r1", "replay", "cap_a", status="success", outputs={"x": "1"})
    runs_mod.record_run("r2", "replay", "cap_b", status="business_outcome",
                        business_outcome_code="NO_MEMBER")
    rows = runs_mod.list_runs()
    assert [r["run_id"] for r in rows] == ["r2", "r1"]
    assert rows[0]["business_outcome_code"] == "NO_MEMBER"
    assert runs_mod.get_run("r1")["capability_id"] == "cap_a"
    assert runs_mod.get_run("nope") is None


def test_record_run_redacts_params_and_outputs(registry):
    runs_mod.record_run("r", "replay", "c", status="success",
                        params={"password": "hunter2", "member_id": "100234"},
                        outputs={"token": "abc", "balance": "$5.00"})
    row = runs_mod.get_run("r")
    assert row["params"]["password"] == "***REDACTED***"
    assert row["params"]["member_id"] == "100234"
    assert row["outputs"]["token"] == "***REDACTED***"
    assert row["outputs"]["balance"] == "$5.00"


def test_list_runs_tolerates_a_corrupt_line(registry):
    registry.write_text('{"run_id": "ok", "status": "success"}\nnot json\n')
    rows = runs_mod.list_runs()
    assert len(rows) == 1 and rows[0]["run_id"] == "ok"


# ---- API ------------------------------------------------------------------------------------

@pytest.fixture
def client(registry, monkeypatch):
    from api import app as api_app

    monkeypatch.setattr(api_app, "record_run", runs_mod.record_run)
    monkeypatch.setattr(api_app, "list_runs", runs_mod.list_runs)
    monkeypatch.setattr(api_app, "get_run", runs_mod.get_run)

    def fake_invoke(cap_id, args):
        return Result(status="success", outputs={"echo": args})

    monkeypatch.setattr(api_app, "invoke_capability", fake_invoke)
    # never launch the operator console in a test
    monkeypatch.setattr(api_app, "_ensure_operator_console", lambda: None)
    api_app.app.config.update(TESTING=True)
    return api_app.app.test_client()


def test_health_and_catalog(client):
    assert client.get("/api/health").get_json()["ok"] is True
    cat = client.get("/api/capabilities").get_json()
    by_name = {t["name"]: t for t in cat}
    assert by_name["meridian_check_member_balance"]["invocable"] is True
    # the session precondition is listed (dashboard shows the full surface) but not invocable
    assert by_name["meridian_signon"]["invocable"] is False
    tool = by_name["meridian_funds_transfer"]
    assert tool["risk_level"] == "risky" and tool["needs_session"] is True
    # `confirm` is never a callable parameter — the risky gate is the operator console, not a flag
    assert "confirm" not in tool["input_schema"]["properties"]


def test_invoke_refuses_a_precondition(client):
    assert client.post("/api/capabilities/meridian_signon/invoke", json={"args": {}}).status_code == 400


def test_invoke_unknown_capability_404(client):
    assert client.post("/api/capabilities/nope/invoke", json={"args": {}}).status_code == 404


def test_invoke_safe_non_session_capability_records_a_run(client):
    resp = client.post("/api/capabilities/lookup_member_balance/invoke",
                       json={"args": {"member_id": "23456"}})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["result"]["status"] == "success"
    assert body["run"]["via"] == "api"
    runs = client.get("/api/runs").get_json()
    assert runs[0]["run_id"] == body["run_id"]
    assert client.get(f"/api/runs/{body['run_id']}").get_json()["capability_id"] == "lookup_member_balance"


def test_run_evidence_rejects_path_traversal(client):
    assert client.get("/api/runs/x/evidence/..%2f..%2fetc%2fpasswd").status_code in (400, 404)
