"""
app2/ - the hostile-DOM target and its ?inject= fault switch.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app2"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from app2.app import app as hostile_app


@pytest.fixture
def c():
    hostile_app.config["TESTING"] = True
    return hostile_app.test_client()


def test_health_is_plain_and_ignores_injection(c):
    assert c.get("/health").data == b"ok"
    assert c.get("/health?inject=maintenance").status_code == 200


def test_find_lists_a_member_and_the_open_link(c):
    r = c.post("/find", data={"q": "12345"})
    assert r.status_code == 200
    assert b"Jordan Lee" in r.data
    assert b'role="link"' in r.data and b"open" in r.data


def test_find_no_match(c):
    assert b"No matching member." in c.post("/find", data={"q": "does-not-exist"}).data


def test_account_balance_uses_a_labelledby_status_span(c):
    r = c.get("/acct/12345")
    assert b'role="status"' in r.data and b"aria-labelledby" in r.data
    assert b"$1,842.30" in r.data
    # no classic label association anywhere
    assert b"<label" not in r.data


def test_locked_member_is_denied(c):
    assert b"This account is restricted." in c.get("/acct/99999").data


def test_missing_member_is_404(c):
    assert c.get("/acct/00000").status_code == 404


def test_inject_maintenance_503s_every_route(c):
    for path in ("/find?inject=maintenance", "/acct/12345?inject=maintenance"):
        r = c.get(path)
        assert r.status_code == 503
        assert b"Service temporarily unavailable" in r.data
        assert r.headers.get("Retry-After")


def test_inject_blank_keeps_the_page_but_empties_the_balance(c):
    r = c.get("/acct/12345?inject=blank")
    assert r.status_code == 200
    assert b"$1,842.30" not in r.data
    assert b'role="status"' in r.data  # element still present -> data_unavailable, not missing


def test_inject_carries_through_links_and_form_actions(c):
    # the search form's action keeps the inject kind so the whole flow stays injected
    assert b'action="/find?inject=blank"' in c.get("/find?inject=blank").data
    # and in-page links carry it too
    assert b"/acct/12345/move?inject=blank" in c.get("/acct/12345?inject=blank").data


def test_unknown_inject_kind_is_ignored(c):
    assert c.get("/find?inject=drop-tables").status_code == 200


def test_move_funds_flow_reaches_a_reference(c):
    r = c.post("/acct/12345/move", data={"stage": "review", "amount": "$5.00", "dest": "ACME-2"})
    assert b"Confirm transfer" in r.data
    r2 = c.post("/acct/12345/move", data={"stage": "confirm", "amount": "$5.00", "dest": "ACME-2"})
    assert b"Transfer reference" in r2.data and b"TR-" in r2.data
