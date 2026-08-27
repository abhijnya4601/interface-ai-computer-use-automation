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


# ---- _resolve_outcome: recoverable recovery (retry / reauth+retry) --------------------------

from replay.engine import _resolve_outcome, _perform_action  # noqa: E402
from artifact.schema import Step, LocatorTarget  # noqa: E402


class _FakePage:
    """A page whose main-frame document status is scripted per navigation."""
    def __init__(self, statuses, content=""):
        self._statuses = list(statuses)
        self._content = content
        self.url = "https://web-sample.interface-hiring.com/members"

    def goto(self, *a, **k):
        self._cur = self._statuses.pop(0) if self._statuses else 200
        class _R:  # noqa
            status = self._cur
        return _R()

    def content(self):
        return self._content

    def wait_for_load_state(self, *a, **k):
        pass


MERIDIAN_TARGET = {"app_name": "meridian-core",
                   "entry_point": "https://web-sample.interface-hiring.com/members",
                   "surface_type": "legacy_web"}


def _nav_step():
    return Step(step_id="s1", action_type="navigate",
               value="https://web-sample.interface-hiring.com/members")


def _run_ctx():
    from surface.outcomes import profile_for
    from artifact.schema import TargetSpec
    return {"profile": profile_for(TargetSpec(**MERIDIAN_TARGET)), "http_status": None}


def test_recovery_retry_clears_a_transient_503_and_continues():
    # 503 on the first check, then the retry navigation returns 200 -> recovered, run continues
    page = _FakePage(statuses=[200])  # the retry goto() returns 200
    ctx = _run_ctx()
    ctx["http_status"] = 503
    from surface.outcomes import classify
    outcome = classify(ctx["profile"], 503, "")
    rec_log = []
    result = _resolve_outcome(outcome, _nav_step(), page, {}, ctx, {}, rec_log, reauth=None)
    assert result is None                       # None => caller continues the run
    assert rec_log == [{"step_id": "s1", "code": "MAINTENANCE", "action": "retry",
                        "attempts": 1, "outcome": "recovered"}]


def test_recovery_retry_gives_up_after_max_attempts():
    page = _FakePage(statuses=[503, 503, 503])  # every retry still 503
    ctx = _run_ctx()
    ctx["http_status"] = 503
    from surface.outcomes import classify
    outcome = classify(ctx["profile"], 503, "")
    rec_log = []
    result = _resolve_outcome(outcome, _nav_step(), page, {}, ctx, {}, rec_log, reauth=None)
    assert result.status == "recoverable_handled"
    assert rec_log[-1]["outcome"] == "gave_up" and rec_log[-1]["attempts"] == 3


def test_recovery_reauth_and_retry_calls_the_reauth_hook():
    page = _FakePage(statuses=[200])
    ctx = _run_ctx()
    ctx["http_status"] = 440
    from surface.outcomes import classify
    outcome = classify(ctx["profile"], 440, "")
    calls = []
    result = _resolve_outcome(outcome, _nav_step(), page, {}, ctx, {}, [],
                              reauth=lambda: calls.append(1))
    assert calls == [1]          # the reauth hook fired
    assert result is None        # then the retry cleared


def test_plain_recoverable_with_no_recovery_hint_still_just_stops():
    outcome = ExpectedOutcome(condition="x", classification="recoverable", code="X")  # no `recovery`
    result = _resolve_outcome(outcome, _nav_step(), None, {}, {}, {}, [], reauth=None)
    assert result.status == "recoverable_handled"
