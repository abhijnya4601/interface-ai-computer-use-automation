"""
Offline unit tests for agent/recorder.py's 3-tier locator builder and parameter detection.
Uses minimal fake Playwright-shaped objects (just .get_by_role(...).count() and .frames /
.main_frame) so the tier-selection logic can be exercised without a real browser — including
tier 2 and tier 3, which this project's real app never actually triggers (every role+name pair
in app/templates/*.html is deliberately unique), so these are the only real proof those branches
work at all.
"""
from agent.recorder import Recorder


class FakeLocator:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class FakeContext:
    def __init__(self, counts: dict[tuple[str, str], int]):
        self._counts = counts

    def get_by_role(self, role, name=None):
        return FakeLocator(self._counts.get((role, name), 0))


class FakePage(FakeContext):
    def __init__(self, counts, extra_frames=None):
        super().__init__(counts)
        self.main_frame = self
        self.frames = [self] + list(extra_frames or [])


def _recorder():
    return Recorder(goal="Look up member 12345 and read their current savings balance.")


def test_unique_role_name_resolves_tier1_role_name():
    page = FakePage({("button", "Go"): 1})
    target = _recorder().build_locator("button", "Go", page, "s1")
    assert target.strategy == "role_name"
    assert target.primary == {"role": "button", "name": "Go"}
    assert target.reasoning  # non-empty, required by schema


def test_duplicate_role_name_resolves_tier2_structural():
    page = FakePage({("link", "View"): 3})
    target = _recorder().build_locator("link", "View", page, "s2")
    assert target.strategy == "structural"
    assert target.primary["nth"] == 0
    assert "not unique" in target.reasoning


def test_unresolvable_role_name_resolves_tier3_text():
    page = FakePage({})
    target = _recorder().build_locator("button", "Nonexistent Button", page, "s3")
    assert target.strategy == "text"
    assert target.primary == {"text": "Nonexistent Button"}


def test_match_inside_child_frame_still_resolves_tier1():
    child_frame = FakeContext({("button", "Confirm and Open Account"): 1})
    page = FakePage({}, extra_frames=[child_frame])
    target = _recorder().build_locator("button", "Confirm and Open Account", page, "s4")
    assert target.strategy == "role_name"


def test_tier_log_records_every_build_locator_call():
    rec = _recorder()
    page = FakePage({("button", "Go"): 1, ("link", "View"): 2})
    rec.build_locator("button", "Go", page, "s1")
    rec.build_locator("link", "View", page, "s2")
    assert [entry["tier"] for entry in rec.tier_log] == ["role_name", "structural"]
    assert [entry["step_id"] for entry in rec.tier_log] == ["s1", "s2"]


def test_step_ids_increment_across_record_calls():
    rec = _recorder()
    page = FakePage({("button", "Go"): 1})
    s1 = rec.record_navigate("http://localhost:5050/search")
    s2 = rec.record_click("button", "Go", page)
    assert s1.step_id == "s1"
    assert s2.step_id == "s2"
    assert rec.steps == [s1, s2]


def test_typed_value_matching_goal_becomes_param_ref():
    rec = _recorder()
    page = FakePage({("textbox", "Search (ID / name)"): 1})
    step = rec.record_type("textbox", "Search (ID / name)", "12345", page)
    assert step.value == {"param_ref": "member_id"}


def test_typed_value_that_merely_appears_as_a_goal_substring_is_not_misdetected():
    """Regression test for a real bug (DECISIONS.md D13): a $50 deposit amount typed while
    recording a goal like "...member 12345 with a $50 opening deposit..." must NOT be tagged as
    the member_id param just because "50" is also a substring of the goal text."""
    rec = Recorder(goal="Open a new sub-account for member 12345 with a $50 opening deposit.")
    page = FakePage({("textbox", "Opening Deposit ($)"): 1})
    step = rec.record_type("textbox", "Opening Deposit ($)", "50", page)
    assert step.value == "50"


def test_member_id_typed_into_a_different_field_is_still_detected_correctly():
    rec = Recorder(goal="Open a new sub-account for member 12345 with a $50 opening deposit.")
    page = FakePage({("textbox", "Search (ID / name)"): 1})
    step = rec.record_type("textbox", "Search (ID / name)", "12345", page)
    assert step.value == {"param_ref": "member_id"}


def test_goal_with_no_member_id_pattern_never_tags_a_param_ref():
    rec = Recorder(goal="Search for the term 50 and click Go.")
    page = FakePage({("textbox", "q"): 1})
    step = rec.record_type("textbox", "q", "50", page)
    assert step.value == "50"


def test_typed_value_not_in_goal_stays_a_literal():
    rec = _recorder()
    page = FakePage({("textbox", "Nickname"): 1})
    step = rec.record_type("textbox", "Nickname", "Holiday", page)
    assert step.value == "Holiday"


def test_record_extract_sets_extract_as():
    rec = _recorder()
    page = FakePage({("rowheader", "Savings Balance"): 1})
    step = rec.record_extract("rowheader", "Savings Balance", "savings_balance", page)
    assert step.action_type == "extract"
    assert step.extract_as == "savings_balance"


# ---- table_position locator (D22) -- guard-clause paths only; the real DOM-walking logic ----
# ---- is verified live in scripts/smoke_test_table_position.py, which needs a real browser ----

def test_table_position_short_circuits_for_non_cell_roles_without_touching_page():
    rec = _recorder()
    # page=None would blow up if the function tried to use it -- proves the role check happens first
    assert rec._try_table_position_locator("button", "Go", None) is None
    assert rec._try_table_position_locator("rowheader", "Savings Balance", None) is None


def test_record_extract_falls_back_to_normal_tiers_when_not_a_table_position_shape():
    rec = _recorder()
    page = FakePage({("rowheader", "Savings Balance"): 1})
    step = rec.record_extract("rowheader", "Savings Balance", "savings_balance", page)
    assert step.target.strategy == "role_name"
