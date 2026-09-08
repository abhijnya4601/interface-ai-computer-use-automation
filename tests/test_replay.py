import pytest

from artifact.schema import ExpectedOutcome, ExtractContract, Step, WaitPolicy
from replay.engine import (
    _await_ready,
    _extract_quoted_substring,
    _locate_table_position,
    _outcome_to_result,
    _resolve_value,
    _validate_extracted,
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


def test_slug_turns_a_field_name_into_a_param_key():
    from replay.engine import _slug
    assert _slug("Opening Deposit ($)") == "opening_deposit"
    assert _slug("Reason for dispute") == "reason_for_dispute"
    assert _slug("ZIP Code") == "zip_code"
    assert _slug("") == ""


def test_resolve_value_fills_a_url_template():
    value = {"param_ref": "member_id", "url_template": "http://h/member/{member_id}/txns"}
    assert _resolve_value(value, {"member_id": "999"}) == "http://h/member/999/txns"


def test_resolve_value_url_template_still_needs_the_param():
    with pytest.raises(KeyError):
        _resolve_value({"param_ref": "member_id", "url_template": "http://h/{member_id}"}, {})


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


def test_outcome_to_result_data_unavailable_from_declared_condition():
    outcome = ExpectedOutcome(
        condition="page contains 'Balance service temporarily unavailable'",
        classification="data_unavailable",
        code="LEDGER_DOWN",
    )
    result = _outcome_to_result(outcome, {"other": "kept"})
    assert result.status == "data_unavailable"
    assert result.business_outcome_code == "LEDGER_DOWN"
    assert result.outputs == {"other": "kept"}


# ---- _validate_extracted -------------------------------------------------------------------

def test_validate_extracted_passes_a_well_shaped_value():
    contract = ExtractContract(pattern=r"\$[\d,]+\.\d{2}")
    assert _validate_extracted("$1,842.30", contract) is None


def test_validate_extracted_flags_empty_value():
    contract = ExtractContract(pattern=r"\$[\d,]+\.\d{2}")
    reason = _validate_extracted("   ", contract)
    assert reason and "empty" in reason


def test_validate_extracted_flags_placeholder():
    contract = ExtractContract(nonempty=True, placeholders=["--", "N/A"])
    reason = _validate_extracted("--", contract)
    assert reason and "placeholder" in reason


def test_validate_extracted_flags_wrong_shape():
    contract = ExtractContract(pattern=r"\d{4}-\d{2}-\d{2}")
    reason = _validate_extracted("No transactions on file.", contract)
    assert reason and "shape" in reason


def test_validate_extracted_treats_a_broken_pattern_as_unchecked_not_a_crash():
    contract = ExtractContract(pattern=r"([unclosed")
    assert _validate_extracted("anything", contract) is None


def test_validate_extracted_allows_empty_when_nonempty_is_false():
    contract = ExtractContract(nonempty=False)
    assert _validate_extracted("", contract) is None


# ---- _await_ready ------------------------------------------------------------------------------

class _ContentPage:
    def __init__(self, content):
        self._content = content

    def content(self):
        return self._content


def _step_with_ready(ready_when, timeout_ms=20):
    return Step(
        step_id="s1", action_type="click", ready_when=ready_when,
        wait_policy=WaitPolicy(timeout_ms=timeout_ms),
    )


def test_await_ready_true_when_no_gate_declared():
    assert _await_ready(_step_with_ready(None), _ContentPage("")) is True


def test_await_ready_true_when_marker_already_present():
    step = _step_with_ready("page contains 'Savings Balance'")
    assert _await_ready(step, _ContentPage("<td>Savings Balance</td>")) is True


def test_await_ready_false_when_marker_never_appears():
    step = _step_with_ready("page contains 'Savings Balance'", timeout_ms=20)
    assert _await_ready(step, _ContentPage("<td>still loading…</td>")) is False


def test_await_ready_true_when_gate_string_has_no_quoted_marker():
    # malformed ready_when -> can't extract a marker -> don't block the run on it
    step = _step_with_ready("page is ready")
    assert _await_ready(step, _ContentPage("")) is True


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
