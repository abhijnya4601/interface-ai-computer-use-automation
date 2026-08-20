"""
Offline tests for the operator console's authentication (D18) — the single most safety-critical
access point in the system (whoever can reach it can approve an irreversible financial action).
Uses Flask's test client, no live server or browser needed.

Credentials are set via env vars BEFORE importing escalation.operator_page, since the module
generates a random password at import time if none is set (fail-secure default) — the test needs
a deterministic credential to exercise both the accept and reject paths.
"""
import base64
import os

os.environ["OPERATOR_USERNAME"] = "test_operator"
os.environ["OPERATOR_PASSWORD"] = "test_password_123"

import pytest

from escalation import controller, operator_page


def _basic_auth_header(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client():
    operator_page.app.config["TESTING"] = True
    return operator_page.app.test_client()


@pytest.fixture(autouse=True)
def _isolated_lease_state(tmp_path, monkeypatch):
    monkeypatch.setattr(controller, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(controller, "LEASE_PATH", tmp_path / "state" / "lease.json")
    monkeypatch.setattr(controller, "RESUME_SIGNAL_PATH", tmp_path / "state" / "resume.signal")
    monkeypatch.setattr(controller, "EVIDENCE_DIR", tmp_path / "evidence")
    yield


def test_index_without_credentials_is_rejected(client):
    resp = client.get("/")
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


def test_index_with_wrong_credentials_is_rejected(client):
    resp = client.get("/", headers=_basic_auth_header("test_operator", "wrong_password"))
    assert resp.status_code == 401


def test_index_with_wrong_username_is_rejected(client):
    resp = client.get("/", headers=_basic_auth_header("someone_else", "test_password_123"))
    assert resp.status_code == 401


def test_index_with_correct_credentials_succeeds(client):
    resp = client.get("/", headers=_basic_auth_header("test_operator", "test_password_123"))
    assert resp.status_code == 200
    assert b"Operator Console" in resp.data


def test_resume_without_credentials_is_rejected_and_does_not_change_lease(client):
    """The critical case: an unauthenticated POST to /resume must NOT be able to approve
    anything, since that's the one action that can let an irreversible step proceed."""
    resp = client.post("/resume", data={"decision": "approved"})
    assert resp.status_code == 401
    assert not controller.RESUME_SIGNAL_PATH.exists()


def test_resume_with_correct_credentials_succeeds_and_writes_the_signal(client):
    resp = client.post(
        "/resume",
        data={"decision": "approved", "summary": "reviewed and approved"},
        headers=_basic_auth_header("test_operator", "test_password_123"),
    )
    assert resp.status_code in (200, 302)
    assert controller.RESUME_SIGNAL_PATH.exists()


def test_screenshot_route_also_requires_auth(client):
    resp = client.get("/screenshot")
    assert resp.status_code == 401


def test_index_response_is_never_cached(client):
    """Found live (D24): a browser back-button or reload could redisplay an already-resolved
    'escalated' view from cache after a real operator had already clicked Approve, making a
    resolved request look like it was still pending. The page shows live state, so it must never
    be served from cache."""
    resp = client.get("/", headers=_basic_auth_header("test_operator", "test_password_123"))
    assert resp.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, max-age=0"


def test_resume_redirects_with_the_decision_for_a_confirmation_banner(client):
    """Same finding: a bare redirect after Approve gave no positive feedback that the click
    actually did anything, which is exactly what made a stale cached page look ambiguous."""
    resp = client.post(
        "/resume",
        data={"decision": "approved", "summary": "reviewed and approved"},
        headers=_basic_auth_header("test_operator", "test_password_123"),
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/?resumed=approved"

    follow = client.get(
        resp.headers["Location"], headers=_basic_auth_header("test_operator", "test_password_123")
    )
    assert b"Resume signal sent" in follow.data
    assert b"decision: approved" in follow.data
