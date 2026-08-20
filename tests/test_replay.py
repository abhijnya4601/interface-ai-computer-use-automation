import pytest

from artifact.schema import ExpectedOutcome
from replay.engine import (
    _extract_quoted_substring,
    _locate_table_position,
    _outcome_to_result,
    _resolve_value,
    _verify_checkpoint,
)


# ---- _resolve_value ---------------------------------------------------------------------------

def test_resolve_value_returns_literal_unchanged():
    assert _resolve_value("christmas_club", {}) == "christmas_club"


def test_resolve_value_resolves_param_ref():
    assert _resolve_value({"param_ref": "member_id"}, {"member_id": "23456"}) == "23456"


def test_resolve_value_missing_param_raises_keyerror():
    with pytest.raises(KeyError):
        _resolve_value({"param_ref": "member_id"}, {})


# ---- _extract_quoted_substring --------------------------------------------------------------

def test_extract_quoted_substring_finds_marker():
    assert _extract_quoted_substring("page contains 'No results.'") == "No results."


def test_extract_quoted_substring_returns_none_without_quotes():
    assert _extract_quoted_substring("no quotes here") is None


# ---- _outcome_to_result -----------------------------------------------------------------------

def test_outcome_to_result_business_outcome():
    outcome = ExpectedOutcome(condition="x", classification="business_outcome", code="MEMBER_NOT_FOUND")
    result = _outcome_to_result(outcome, {})
    assert result.status == "business_outcome"
    assert result.business_outcome_code == "MEMBER_NOT_FOUND"


def test_outcome_to_result_recoverable():
    outcome = ExpectedOutcome(condition="x", classification="recoverable", handling="dismiss and retry")
    result = _outcome_to_result(outcome, {"partial": "data"})
    assert result.status == "recoverable_handled"
    assert result.outputs == {"partial": "data"}


def test_outcome_to_result_hard_failure_from_declared_condition():
    outcome = ExpectedOutcome(condition="page contains 'Internal Server Error'", classification="hard_failure")
    result = _outcome_to_result(outcome, {})
    assert result.status == "hard_failure"
    assert result.failure_detail["observed"] == outcome.condition


# ---- _verify_checkpoint -----------------------------------------------------------------------

class FakeLocator:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class FakeContext:
    def __init__(self, role_counts=None, content_text=""):
        self._role_counts = role_counts or {}
        self._content_text = content_text

    def get_by_role(self, role, name=None):
        return FakeLocator(self._role_counts.get((role, name), 0))

    def content(self):
        return self._content_text


class FakePage(FakeContext):
    def __init__(self, url="", **kwargs):
        super().__init__(**kwargs)
        self.url = url
        self.main_frame = self
        self.frames = [self]


class _Checkpoint:
    def __init__(self, type, locator=None, expected=""):
        self.type = type
        self.locator = locator
        self.expected = expected


class _Capability:
    def __init__(self, checkpoint):
        self.checkpoint = checkpoint


def test_verify_checkpoint_url_match_true():
    cap = _Capability(_Checkpoint(type="url_match", expected="/member/12345"))
    page = FakePage(url="http://localhost:5050/member/12345")
    assert _verify_checkpoint(cap, page) is True


def test_verify_checkpoint_url_match_false():
    cap = _Capability(_Checkpoint(type="url_match", expected="/member/99999"))
    page = FakePage(url="http://localhost:5050/member/12345")
    assert _verify_checkpoint(cap, page) is False


def test_verify_checkpoint_text_match():
    cap = _Capability(_Checkpoint(type="text_match", expected="Confirm and Open Account"))
    page = FakePage(content_text="<button>Confirm and Open Account</button>")
    assert _verify_checkpoint(cap, page) is True


def test_verify_checkpoint_element_present_true():
    cap = _Capability(_Checkpoint(type="element_present", locator={"role": "rowheader", "name": "Savings Balance"}))
    page = FakePage(role_counts={("rowheader", "Savings Balance"): 1})
    assert _verify_checkpoint(cap, page) is True


def test_verify_checkpoint_element_present_false():
    cap = _Capability(_Checkpoint(type="element_present", locator={"role": "rowheader", "name": "Savings Balance"}))
    page = FakePage(role_counts={})
    assert _verify_checkpoint(cap, page) is False


# ---- _locate_table_position -- guard-clause paths; the real DOM-walking logic is ----
# ---- verified live in scripts/smoke_test_table_position.py, which needs a real browser ----

def test_locate_table_position_returns_none_with_no_headers():
    # page=None would blow up if this reached real DOM logic -- proves the guard fires first
    assert _locate_table_position(None, {"row_index": 0, "column_index": 0}) is None


def test_locate_table_position_returns_none_with_missing_row_index():
    assert _locate_table_position(None, {"table_headers": ["Date"], "column_index": 0}) is None


def test_locate_table_position_returns_none_with_missing_column_index():
    assert _locate_table_position(None, {"table_headers": ["Date"], "row_index": 0}) is None
