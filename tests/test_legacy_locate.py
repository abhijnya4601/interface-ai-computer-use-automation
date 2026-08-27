"""
Offline unit tests for agent/legacy_locate.py (the label-less legacy-form adapter) and
agent/perception.py's _enrich_unnamed_controls, using fake Playwright-shaped stand-ins. The
live half — that this actually drives MERIDIAN CORE's login/search/transfer forms — is
scripts/smoke_meridian_signon.py.
"""
import pytest

from agent.legacy_locate import (
    _role_self_predicate,
    _xpath_label_predicate,
    locate_field_name,
    locate_labeled_field,
    locate_labeled_value,
    normalize_label,
)
from agent.perception import _collect_nameless_control_nodes, _enrich_unnamed_controls


# ---- normalize_label ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Operator ID:", "Operator ID"),
    ("  Amount:  ", "Amount"),
    ("* Reason Code:", "Reason Code"),
    ("From Share", "From Share"),
    ("E-mail:", "E-mail"),
    ("", ""),
    (None, ""),
])
def test_normalize_label(raw, expected):
    assert normalize_label(raw) == expected


# ---- xpath predicate builders ----------------------------------------------------------------

def test_xpath_label_predicate_strips_colon_and_star_in_xpath():
    pred = _xpath_label_predicate("Amount")
    assert "translate(., ':*', '')" in pred
    assert "'Amount'" in pred


def test_xpath_label_predicate_drops_apostrophe_rather_than_breaking_the_expression():
    pred = _xpath_label_predicate("Member's Name")
    assert "Members Name" in pred  # apostrophe dropped, not left to break the string literal
    assert "Member's" not in pred


@pytest.mark.parametrize("role,expected", [
    ("combobox", "self::select"),
    ("listbox", "self::select"),
    ("textbox", "self::input or self::textarea"),
    ("searchbox", "self::input or self::textarea"),
    ("checkbox", "self::input"),
    (None, "self::input or self::select or self::textarea"),
    ("weird", "self::input or self::select or self::textarea"),
])
def test_role_self_predicate(role, expected):
    assert _role_self_predicate(role) == expected


# ---- fake Playwright context ---------------------------------------------------------------

class FakeLocator:
    def __init__(self, count, name_attr=None):
        self._count = count
        self._name_attr = name_attr

    def count(self):
        return self._count

    @property
    def first(self):
        return self

    def get_attribute(self, attr):
        return self._name_attr if attr == "name" else None


class FakeCtx:
    """Returns a canned FakeLocator per selector substring it's asked for."""
    def __init__(self, by_selector_substring: dict, evaluate_result=None):
        self._map = by_selector_substring
        self._evaluate_result = evaluate_result or []

    def locator(self, selector):
        for needle, loc in self._map.items():
            if needle in selector:
                return loc
        return FakeLocator(0)

    def evaluate(self, _js):
        return self._evaluate_result


# ---- locate_labeled_field ------------------------------------------------------------------

def test_locate_labeled_field_returns_single_row_scoped_match():
    ctx = FakeCtx({"following-sibling::*": FakeLocator(1, name_attr="operator")})
    loc = locate_labeled_field(ctx, "Operator ID:", control_role="textbox")
    assert loc is not None
    assert loc.get_attribute("name") == "operator"


def test_locate_labeled_field_skips_ambiguous_candidate_and_tries_the_next():
    # first (row-sibling) candidate matches 3 controls -> skip; a later candidate matches 1
    ctx = FakeCtx({
        "following-sibling::*": FakeLocator(3),
        "following::*[self::input or self::textarea][1]": FakeLocator(1, name_attr="q"),
    })
    loc = locate_labeled_field(ctx, "Value", control_role="textbox")
    assert loc is not None and loc.get_attribute("name") == "q"


def test_locate_labeled_field_none_when_nothing_resolves():
    assert locate_labeled_field(FakeCtx({}), "Nonexistent", "textbox") is None


def test_locate_labeled_field_none_on_empty_label():
    assert locate_labeled_field(FakeCtx({"x": FakeLocator(1)}), "   ", "textbox") is None


# ---- locate_field_name -------------------------------------------------------------------------

def test_locate_field_name_hits_on_name_attribute_selector():
    ctx = FakeCtx({'[name="amount"]': FakeLocator(1)})
    assert locate_field_name(ctx, "amount") is not None


def test_locate_field_name_none_for_missing_or_empty():
    assert locate_field_name(FakeCtx({}), "amount") is None
    assert locate_field_name(FakeCtx({'[name="x"]': FakeLocator(1)}), None) is None


# ---- locate_labeled_value -------------------------------------------------------------------

def test_locate_labeled_value_returns_sibling_cell_of_a_lbl_label():
    ctx = FakeCtx({"following-sibling::*[self::td or self::th][1]": FakeLocator(1)})
    assert locate_labeled_value(ctx, "Confirmation:") is not None


def test_locate_labeled_value_skips_ambiguous_and_tries_next():
    ctx = FakeCtx({
        "following-sibling::*[self::td or self::th][1]": FakeLocator(2),
        "/*[self::td or self::th][last()]": FakeLocator(1),
    })
    assert locate_labeled_value(ctx, "Amount") is not None


def test_locate_labeled_value_none_on_empty_label():
    assert locate_labeled_value(FakeCtx({"x": FakeLocator(1)}), "  :  ") is None


# ---- perception enrichment -------------------------------------------------------------------

def _tree_with_nameless_controls():
    return {
        "role": "document",
        "children": [
            {"role": "textbox"},
            {"role": "textbox"},
            {"role": "combobox", "children": [{"role": "option", "name": "MAIN-001"}]},
            {"role": "button", "name": "Sign On"},
        ],
    }


class FakePage:
    def __init__(self, fields):
        self._fields = fields
        self.main_frame = object()
        self.frames = [self.main_frame]

    def evaluate(self, _js):
        return self._fields


def test_collect_nameless_control_nodes_ignores_named_and_non_controls():
    nodes = _collect_nameless_control_nodes(_tree_with_nameless_controls())
    assert [n["role"] for n in nodes] == ["textbox", "textbox", "combobox"]


def test_enrich_assigns_derived_labels_in_document_order():
    tree = _tree_with_nameless_controls()
    page = FakePage([
        {"label": "Operator ID", "name": "operator"},
        {"label": "Password", "name": "password"},
        {"label": "Branch", "name": "branch"},
    ])
    # main frame path: perception iterates page.frames; FakePage.main_frame has no .evaluate,
    # so only the page-level derive call contributes — which is what we assert against.
    _enrich_unnamed_controls(page, tree)
    kids = tree["children"]
    assert kids[0]["name"] == "Operator ID"
    assert kids[1]["name"] == "Password"
    assert kids[2]["name"] == "Branch"
    assert kids[3]["name"] == "Sign On"  # untouched


def test_enrich_only_fills_aligned_prefix_when_lists_mismatch():
    tree = _tree_with_nameless_controls()
    page = FakePage([{"label": "Operator ID", "name": "operator"}])  # only one field derived
    _enrich_unnamed_controls(page, tree)
    kids = tree["children"]
    assert kids[0]["name"] == "Operator ID"
    assert "name" not in kids[1]  # nothing to align it with -> left nameless, not mis-guessed


def test_enrich_is_a_noop_when_no_fields_derived():
    tree = _tree_with_nameless_controls()
    _enrich_unnamed_controls(FakePage([]), tree)
    assert "name" not in tree["children"][0]
