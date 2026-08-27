"""
Offline unit tests for agent/session.py — credential resolution from the environment and the
run_with_session control flow (signon-fails / missing-creds paths), with replay() monkeypatched
so no browser launches. The live half is scripts/smoke_meridian_session.py.
"""
import pytest

import agent.session as session
from agent.session import MissingCredentials, credentials_for, run_with_session
from artifact.schema import Capability, Checkpoint, Result, Step

_TARGET = Capability(
    capability_id="t", version="1.0.0", created_from_run_id="x", description="",
    target={"app_name": "meridian-core", "entry_point": "https://web-sample.interface-hiring.com/x",
            "surface_type": "legacy_web"},
    risk_level="safe", input_schema={}, output_schema={},
    checkpoint=Checkpoint(type="url_match", locator=None, expected="x"),
    steps=[Step(step_id="s1", action_type="navigate", value="https://web-sample.interface-hiring.com/x")],
)


@pytest.fixture(autouse=True)
def _clear_meridian_env(monkeypatch):
    for var in ("MERIDIAN_OPERATOR", "MERIDIAN_PASSWORD", "MERIDIAN_BRANCH",
                "MERIDIAN_SUPERVISOR_OPERATOR", "MERIDIAN_SUPERVISOR_PASSWORD",
                "MERIDIAN_SUPERVISOR_BRANCH"):
        monkeypatch.delenv(var, raising=False)


def test_credentials_for_reads_env_and_defaults_branch(monkeypatch):
    monkeypatch.setenv("MERIDIAN_OPERATOR", "teller1")
    monkeypatch.setenv("MERIDIAN_PASSWORD", "pw")
    creds = credentials_for("teller")
    assert creds == {"operator": "teller1", "password": "pw", "branch": "MAIN-001"}


def test_credentials_for_uses_explicit_branch(monkeypatch):
    monkeypatch.setenv("MERIDIAN_OPERATOR", "teller1")
    monkeypatch.setenv("MERIDIAN_PASSWORD", "pw")
    monkeypatch.setenv("MERIDIAN_BRANCH", "WEST-014")
    assert credentials_for("teller")["branch"] == "WEST-014"


def test_credentials_for_missing_raises():
    with pytest.raises(MissingCredentials):
        credentials_for("teller")


def test_credentials_for_unknown_role_raises():
    with pytest.raises(MissingCredentials):
        credentials_for("root")


def test_run_with_session_missing_creds_escalates_not_crashes():
    # a capability requiring a role we have no credentials for -> escalate (a human supplies
    # them or runs it), not a crash and not a plain hard_failure
    result = run_with_session(_TARGET, params={}, role="supervisor")
    assert result.status == "escalated"
    assert "credentials" in str(result.failure_detail)


def test_run_with_session_signon_failure_short_circuits(monkeypatch):
    monkeypatch.setenv("MERIDIAN_OPERATOR", "teller1")
    monkeypatch.setenv("MERIDIAN_PASSWORD", "pw")
    monkeypatch.setattr(session, "load_signon_capability", lambda *a, **k: _TARGET)

    calls = []

    class _FakeBrowser:
        def new_page(self):
            return object()

        def close(self):
            pass

    class _FakeChromium:
        def launch(self, headless=True):
            return _FakeBrowser()

    class _FakePW:
        chromium = _FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(session, "sync_playwright", lambda: _FakePW())

    def fake_replay(cap, params, **kw):
        calls.append(kw.get("run_id", ""))
        # first call is the signon replay -> make it fail
        return Result(status="hard_failure", failure_detail={"step_id": "s5", "expected": "menu",
                                                             "observed": "still on /signon"})

    monkeypatch.setattr(session, "replay", fake_replay)

    result = run_with_session(_TARGET, params={}, role="teller", run_id="r1")
    assert result.status == "hard_failure"
    assert "sign-on replay returned hard_failure" in str(result.failure_detail)
    # target replay must NOT have been attempted after signon failed
    assert calls == ["r1_signon"]
