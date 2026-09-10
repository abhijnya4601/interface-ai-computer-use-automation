"""
The web console's HTTP surface - auth gate, /run validation, /stream sentinel, /resume, /stop,
and the LiveRun hook. The actual browser run is stubbed; there's no Playwright here.
"""
import json
import os

import pytest

import webconsole.runner as runner
import webconsole.server as srv


@pytest.fixture
def client():
    srv.app.config["TESTING"] = True
    return srv.app.test_client()


KEY = srv.SECRET


# ---- auth gate ------------------------------------------------------------------------------

def test_health_is_open(client):
    assert client.get("/health").data == b"ok"


def test_index_without_key_is_403(client):
    assert client.get("/").status_code == 403


def test_index_with_key_works_and_sets_cookie(client):
    r = client.get(f"/?key={KEY}", follow_redirects=False)
    # first hit redirects to drop the key from the address bar
    assert r.status_code in (302, 200)
    assert any("ck=" in h for h in r.headers.getlist("Set-Cookie"))


def test_wrong_key_is_403(client):
    assert client.get("/?key=nope").status_code == 403


# ---- /run validation ---------------------------------------------------------------------

def _post(client, body):
    return client.post(f"/run?key={KEY}", data=json.dumps(body), content_type="application/json")


def test_run_rejects_unknown_mode(client):
    assert _post(client, {"mode": "wat"}).status_code == 400


def test_run_replay_needs_a_real_capability_path(client):
    r = _post(client, {"mode": "replay", "capability": "capabilities/does-not-exist.json"})
    assert r.status_code == 400


def test_run_discovery_without_any_key_is_400(client, monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    r = _post(client, {"mode": "discovery", "goal": "look up member 12345"})
    assert r.status_code == 400
    assert b"needs an API key" in r.data


def test_run_discovery_needs_a_goal(client):
    r = _post(client, {"mode": "discovery", "goal": "   ", "api_key": "sk-ant-whatever"})
    assert r.status_code == 400


def test_run_discovery_rejects_a_bogus_byo_key_on_auto_provider(client):
    # provider left on auto -> an unrecognisable key shape is rejected with a hint
    r = _post(client, {"mode": "discovery", "goal": "look up member 12345", "api_key": "hunter2"})
    assert r.status_code == 400
    assert b"which provider" in r.data


def test_run_discovery_trusts_any_key_when_the_provider_is_explicit(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(runner, "start", lambda kind, **kw: seen.update(kw) or type("R", (), {"id": "x"})())
    # a Google key in the newer AQ.<...> format, with the provider explicitly set
    r = _post(client, {"mode": "discovery", "goal": "look up member 12345",
                       "api_key": "AQ.Ab8RN6xxxxx", "provider": "gemini"})
    assert r.status_code == 200
    assert seen["api_key"] == "AQ.Ab8RN6xxxxx"
    assert seen["provider"] == "gemini"


def test_run_discovery_with_a_byo_key_starts(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(runner, "start", lambda kind, **kw: seen.update(kind=kind, kw=kw)
                        or type("R", (), {"id": "live_x"})())
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    r = _post(client, {"mode": "discovery", "goal": "look up member 12345",
                       "api_key": "sk-ant-abc123", "capability_id": "my_lookup", "provider": "auto"})
    assert r.status_code == 200
    assert seen["kind"] == "discovery"
    assert seen["kw"]["api_key"] == "sk-ant-abc123"
    assert seen["kw"]["provider"] == "auto"


def test_run_discovery_accepts_an_openai_and_a_gemini_key(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(runner, "start", lambda kind, **kw: seen.update(kw) or type("R", (), {"id": "x"})())
    for k in ("sk-proj-abc", "AIzaSyABC"):
        r = _post(client, {"mode": "discovery", "goal": "look up member 12345", "api_key": k})
        assert r.status_code == 200, k
    assert seen["api_key"] == "AIzaSyABC"


# ---- ask mode ---------------------------------------------------------------------------------

def test_run_ask_needs_a_request(client):
    r = _post(client, {"mode": "ask", "request": "  ", "api_key": "sk-ant-x"})
    assert r.status_code == 400


def test_run_ask_needs_a_key(client, monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    r = _post(client, {"mode": "ask", "request": "what's the balance for member 12345?"})
    assert r.status_code == 400
    assert b"needs an API key" in r.data


def test_run_ask_starts_and_forwards_request_and_provider(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(runner, "start", lambda kind, **kw: seen.update(kind=kind, kw=kw)
                        or type("R", (), {"id": "live_a"})())
    r = _post(client, {"mode": "ask", "request": "look up member 12345",
                       "api_key": "sk-ant-abc", "provider": "anthropic"})
    assert r.status_code == 200
    assert seen["kind"] == "ask"
    assert seen["kw"]["request"] == "look up member 12345"
    assert seen["kw"]["provider"] == "anthropic"
    # ask can fall back to discovery, so it needs the target + app name like discovery does
    assert seen["kw"]["target_url"].endswith("/search")
    assert seen["kw"]["app_name"] == "mock-core-banking"


def test_ask_tool_list_includes_the_discover_escape_hatch():
    from agent_interface.catalog import build_tool_catalog
    from webconsole.runner import _DISCOVER_TOOL
    names = [t["name"] for t in build_tool_catalog()] + [_DISCOVER_TOOL["name"]]
    assert "discover_new_capability" in names
    assert "lookup_member_balance" in names


# ---- second target + fault injection --------------------------------------------------------

def test_catalog_tags_each_capability_with_its_app(client):
    caps = client.get(f"/catalog?key={KEY}").get_json()["capabilities"]
    by_id = {c["id"]: c for c in caps}
    assert by_id["lookup_member_balance"]["app"] == "mock-core-banking"
    assert by_id["hostile_check_balance"]["app"] == "hostile-dom"


def test_run_rejects_inject_on_a_target_that_does_not_support_it(client):
    r = _post(client, {"mode": "replay", "target": "mock-core-banking",
                       "capability": "capabilities/lookup_member_balance.v1.json",
                       "inject": "maintenance"})
    assert r.status_code == 400


def test_run_rejects_an_unknown_inject_kind(client):
    r = _post(client, {"mode": "replay", "target": "hostile-dom",
                       "capability": "capabilities/hostile_check_balance.v1.json",
                       "inject": "rm-rf"})
    assert r.status_code == 400


def test_run_replay_forwards_inject_for_the_hostile_target(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(runner, "start", lambda kind, **kw: seen.update(kw) or type("R", (), {"id": "x"})())
    r = _post(client, {"mode": "replay", "target": "hostile-dom",
                       "capability": "capabilities/hostile_check_balance.v1.json",
                       "params": {"member_id": "12345"}, "inject": "blank"})
    assert r.status_code == 200
    assert seen["inject"] == "blank"


def test_run_ask_appends_inject_to_the_target_url(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(runner, "start", lambda kind, **kw: seen.update(kw) or type("R", (), {"id": "x"})())
    r = _post(client, {"mode": "ask", "target": "hostile-dom", "request": "check balance for 12345",
                       "api_key": "sk-ant-x", "inject": "maintenance"})
    assert r.status_code == 200
    assert seen["target_url"].endswith("/find?inject=maintenance")


def test_hostile_capabilities_are_present_and_target_the_hostile_app():
    from artifact.schema import Capability
    for cid, risk in [("hostile_check_balance", "safe"), ("hostile_move_funds", "risky")]:
        p = runner.REPO / f"capabilities/{cid}.v1.json"
        cap = Capability.model_validate_json(p.read_text())
        assert cap.target.app_name == "hostile-dom"
        assert cap.risk_level == risk
        assert cap.steps[0].action_type == "navigate"  # inject is appended here


# ---- /runs (History) ------------------------------------------------------------------------

def test_runs_endpoint_is_gated_and_returns_a_list(client, tmp_path, monkeypatch):
    assert client.get("/runs").status_code == 403
    import agent_interface.runs as runs_mod
    monkeypatch.setattr(runs_mod, "REGISTRY_PATH", tmp_path / "runs.jsonl")
    runs_mod.record_run("r1", "replay", "lookup_member_balance", status="success", started_at=1.0)
    j = client.get(f"/runs?key={KEY}").get_json()
    assert j["runs"][0]["run_id"] == "r1"
    assert client.get(f"/runs?key={KEY}&id=r1").get_json()["capability_id"] == "lookup_member_balance"
    assert client.get(f"/runs?key={KEY}&id=nope").status_code == 404


def test_catalog_endpoint_returns_json_capabilities(client):
    j = client.get(f"/catalog?key={KEY}").get_json()
    assert any(c["id"] == "lookup_member_balance" for c in j["capabilities"])


def test_run_replay_starts_and_returns_a_run_id(client, monkeypatch):
    seen = {}

    class _FakeRun:
        id = "live_test123"

    def _fake_start(kind, **kw):
        seen["kind"] = kind
        seen["kw"] = kw
        return _FakeRun()

    monkeypatch.setattr(runner, "start", _fake_start)
    r = _post(client, {"mode": "replay",
                       "capability": "capabilities/lookup_member_balance.v1.json",
                       "params": {"member_id": "99999"},
                       "overrides": {"s10": "750", "s9": ""},   # blank dropped
                       "confirm": False})
    assert r.status_code == 200
    assert r.get_json()["run_id"] == "live_test123"
    assert seen["kind"] == "replay"
    assert seen["kw"]["params"] == {"member_id": "99999"}
    assert seen["kw"]["overrides"] == {"s10": "750"}


def test_catalog_entries_list_overridable_literal_fields():
    row = next(c for c in runner.catalog()["capabilities"] if c["id"] == "open_subaccount")
    fields = {o["field"] for o in row["overridable"]}
    assert "Opening Deposit ($)" in fields
    assert "Search (ID / name)" not in fields  # that one is a param_ref, not a literal
    o = next(o for o in row["overridable"] if o["field"] == "Opening Deposit ($)")
    assert o["step_id"].startswith("s") and isinstance(o["value"], str)


def test_run_returns_409_when_a_run_is_already_active(client, monkeypatch):
    def _busy(kind, **kw):
        raise RuntimeError("a run is already in progress")

    monkeypatch.setattr(runner, "start", _busy)
    r = _post(client, {"mode": "replay",
                       "capability": "capabilities/lookup_member_balance.v1.json"})
    assert r.status_code == 409


# ---- /resume and /stop -------------------------------------------------------------------

def test_resume_forwards_the_decision_to_the_controller(client, monkeypatch):
    got = {}
    monkeypatch.setattr(srv.controller, "signal_resume",
                        lambda note="", decision=None: got.update(note=note, decision=decision))
    r = client.post(f"/resume?key={KEY}", data=json.dumps({"decision": "approved", "note": "ok"}),
                    content_type="application/json")
    assert r.status_code == 200
    assert got == {"note": "ok", "decision": "approved"}


def test_resume_rejects_a_bad_decision(client):
    r = client.post(f"/resume?key={KEY}", data=json.dumps({"decision": "maybe"}),
                    content_type="application/json")
    assert r.status_code == 400


def test_stop_calls_stop_on_the_current_run(client, monkeypatch):
    stopped = {}

    class _R:
        def stop(self):
            stopped["v"] = True

    monkeypatch.setattr(runner, "current", lambda: _R())
    assert client.post(f"/stop?key={KEY}").get_json()["ok"] is True
    assert stopped["v"] is True


def test_stream_with_no_run_returns_a_done_sentinel(client, monkeypatch):
    monkeypatch.setattr(runner, "current", lambda: None)
    data = client.get(f"/stream?key={KEY}").data.decode()
    assert "event: done" in data


# ---- /rules (escalation policy editor) -------------------------------------------------

@pytest.fixture
def isolated_rules(monkeypatch, tmp_path):
    p = tmp_path / "rules.yaml"
    monkeypatch.setattr(srv.esc_policy, "RULES_PATH", p)
    return p


def test_rules_get_lists_current_rules(client, isolated_rules):
    isolated_rules.write_text("rules:\n  - {id: r1, when: {risk_level: risky}, then: escalate}\n")
    j = client.get(f"/rules?key={KEY}").get_json()
    assert [r["id"] for r in j["rules"]] == ["r1"]


def test_rules_post_appends_and_get_reflects_it(client, isolated_rules):
    r = client.post(f"/rules?key={KEY}",
                    data=json.dumps({"when": {"step_name_contains": ["Confirm"]}, "then": "escalate"}),
                    content_type="application/json")
    assert r.status_code == 200
    rules = r.get_json()["rules"]
    assert len(rules) == 1 and rules[0]["then"] == "escalate"


def test_rules_post_rejects_a_bad_then(client, isolated_rules):
    r = client.post(f"/rules?key={KEY}",
                    data=json.dumps({"when": {"risk_level": "risky"}, "then": "nope"}),
                    content_type="application/json")
    assert r.status_code == 400


def test_rules_post_rejects_an_empty_when(client, isolated_rules):
    r = client.post(f"/rules?key={KEY}", data=json.dumps({"when": {}, "then": "escalate"}),
                    content_type="application/json")
    assert r.status_code == 400


def test_rules_delete_removes_by_id(client, isolated_rules):
    isolated_rules.write_text(
        "rules:\n  - {id: keep, when: {risk_level: risky}, then: escalate}\n"
        "  - {id: drop, when: {risk_level: safe}, then: block}\n")
    j = client.delete(f"/rules?key={KEY}&id=drop").get_json()
    assert [r["id"] for r in j["rules"]] == ["keep"]


# ---- catalog + LiveRun hook -------------------------------------------------------------

def test_unique_capability_id_keeps_a_fresh_name_but_suffixes_a_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "CAPS_DIR", tmp_path)
    assert runner._unique_capability_id("brand_new") == "brand_new"
    (tmp_path / "taken.v1.json").write_text("{}")
    got = runner._unique_capability_id("taken")
    assert got.startswith("taken__") and got != "taken"


def test_capabilities_dir_is_overridable_by_env(tmp_path):
    # checked in a fresh interpreter so reloading the module can't leak into the rest of the suite
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c",
         "import agent.compiler as c; print(c.CAPABILITIES_DIR)"],
        cwd=str(runner.REPO), env={"CAPABILITIES_DIR": str(tmp_path), "PATH": os.environ["PATH"]},
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == str(tmp_path)


def test_catalog_lists_the_real_capabilities_with_their_inputs():
    cat = runner.catalog()
    ids = {c["id"] for c in cat["capabilities"]}
    assert "lookup_member_balance" in ids
    row = next(c for c in cat["capabilities"] if c["id"] == "lookup_member_balance")
    assert "member_id" in row["inputs"]
    assert row["risk"] in ("safe", "risky")


class _FakePage:
    url = "http://localhost:5050/member/99999"

    def screenshot(self, **kw):
        return b"\xff\xd8\xff\xd9"  # minimal jpeg-ish bytes


def test_liverun_hook_emits_agent_then_frame_and_honours_stop():
    run = runner.LiveRun()
    cb = run._hook(_FakePage())
    cb({"type": "step", "step_id": "s1", "action_type": "click"})
    kinds = []
    while not run.events.empty():
        kinds.append(run.events.get_nowait()["type"])
    assert kinds == ["agent", "frame"]

    run.stop()
    with pytest.raises(runner._Stopped):
        cb({"type": "step", "step_id": "s2"})


def test_liverun_hook_drops_the_bulky_accessibility_tree():
    run = runner.LiveRun()
    cb = run._hook(_FakePage())
    cb({"type": "observation", "url": "u", "accessibility_tree": {"huge": [1, 2, 3]}})
    ev = run.events.get_nowait()
    assert ev["type"] == "agent"
    assert "accessibility_tree" not in ev["entry"]


def test_redacted_outputs_masks_and_emits_a_report_event():
    run = runner.LiveRun()
    safe = run._redacted_outputs({"contact_email": "jane.doe@example.com", "member_id": "12345"})
    # gray-area PII tokenised for the evidence sink; the bare id is left intact (flagged)
    assert "jane.doe@example.com" not in str(safe)
    assert safe["member_id"] == "12345"
    ev = run.events.get_nowait()
    assert ev["type"] == "agent" and ev["entry"]["type"] == "redaction"
    assert ev["entry"]["sink"] == "evidence"
    assert "backend" in ev["entry"]


def test_redacted_outputs_emits_nothing_when_there_is_nothing_to_redact():
    run = runner.LiveRun()
    run._redacted_outputs({"balance": "1234.56", "status": "open"})
    assert run.events.empty()
