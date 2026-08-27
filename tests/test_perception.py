"""
Offline (no-browser) unit tests for agent/perception.py's pure functions:
_parse_aria_snapshot (YAML text -> {role,name,value,children} tree) and
prune_accessibility_tree (tree -> pruned tree). Neither needs Playwright — see
scripts/verify_perception_live.py for the live half that does.
"""
from agent.perception import _find_nodes_by_role, _parse_aria_snapshot, prune_accessibility_tree
from tests.fixtures.accessibility_trees import (
    CONFIRM_FRAME_ARIA_YAML,
    CONFIRM_WRAPPER_TOP_LEVEL_YAML,
    DEEPLY_NESTED_TREE,
    SEARCH_PAGE_ARIA_YAML,
    SEARCH_RESULTS_ARIA_YAML,
    TREE_WITH_DECORATIVE_WRAPPERS,
)


# ---- _parse_aria_snapshot -------------------------------------------------------------------

def test_parse_search_page_finds_button_and_textbox():
    tree = _parse_aria_snapshot(SEARCH_PAGE_ARIA_YAML)
    buttons = _find_nodes_by_role(tree, "button")
    textboxes = _find_nodes_by_role(tree, "textbox")
    assert any(b.get("name") == "Go" for b in buttons)
    assert any(t.get("name") == "Search (ID / name)" for t in textboxes)


def test_parse_textbox_with_value_captures_value():
    tree = _parse_aria_snapshot(SEARCH_RESULTS_ARIA_YAML)
    textboxes = _find_nodes_by_role(tree, "textbox")
    assert any(t.get("value") == "12345" for t in textboxes)


def test_parse_search_results_finds_table_rows_and_links():
    tree = _parse_aria_snapshot(SEARCH_RESULTS_ARIA_YAML)
    links = _find_nodes_by_role(tree, "link")
    assert {l.get("name") for l in links} == {"View", "View"} or "View" in {l.get("name") for l in links}
    rowgroups = _find_nodes_by_role(tree, "rowgroup")
    assert len(rowgroups) >= 2  # header rowgroup + data rowgroup, nested-table layout preserved


def test_parse_drops_link_url_metadata_children():
    tree = _parse_aria_snapshot(SEARCH_RESULTS_ARIA_YAML)
    links = _find_nodes_by_role(tree, "link")
    for link in links:
        for child in link.get("children", []):
            assert child.get("role") != "/url"


def test_parse_confirm_frame_finds_confirm_button():
    tree = _parse_aria_snapshot(CONFIRM_FRAME_ARIA_YAML)
    buttons = _find_nodes_by_role(tree, "button")
    assert any(b.get("name") == "Confirm and Open Account" for b in buttons)


def test_parse_top_level_wrapper_does_not_reach_iframe_content():
    """Documents the real, verified limitation build_observation works around."""
    tree = _parse_aria_snapshot(CONFIRM_WRAPPER_TOP_LEVEL_YAML)
    iframe_nodes = _find_nodes_by_role(tree, "iframe")
    assert len(iframe_nodes) == 1
    assert not iframe_nodes[0].get("children")
    assert not _find_nodes_by_role(tree, "button")  # "Confirm and Open Account" not reachable here


# ---- prune_accessibility_tree ---------------------------------------------------------------

def test_prune_keeps_only_role_name_value_children_keys():
    raw = {"role": "button", "name": "Go", "irrelevant_key": "should be dropped"}
    pruned = prune_accessibility_tree(raw)
    assert set(pruned.keys()) <= {"role", "name", "value", "children"}
    assert pruned == {"role": "button", "name": "Go"}


def test_prune_respects_max_depth():
    pruned = prune_accessibility_tree(DEEPLY_NESTED_TREE, max_depth=2)
    buttons = _find_nodes_by_role(pruned, "button")
    assert buttons == []  # buried 4 levels deep, cut off before it's reached


def test_prune_keeps_buried_node_when_depth_allows():
    pruned = prune_accessibility_tree(DEEPLY_NESTED_TREE, max_depth=15)
    buttons = _find_nodes_by_role(pruned, "button")
    assert any(b.get("name") == "buried button" for b in buttons)


def test_prune_drops_empty_decorative_wrappers_but_keeps_meaningful_nodes():
    pruned = prune_accessibility_tree(TREE_WITH_DECORATIVE_WRAPPERS)
    roles_and_names = [(c["role"], c.get("name")) for c in pruned["children"]]
    assert ("button", "Go") in roles_and_names
    assert ("generic", "labelled wrapper") in roles_and_names
    assert not any(r == "presentation" for r, _ in roles_and_names)
    assert len(roles_and_names) == 2  # empty generic and presentation both dropped


def test_prune_handles_missing_children_key_without_crashing():
    pruned = prune_accessibility_tree({"role": "button", "name": "Go"})
    assert pruned == {"role": "button", "name": "Go"}


def test_prune_handles_explicit_empty_children_list_without_crashing():
    pruned = prune_accessibility_tree({"role": "generic", "children": []})
    # a childless, nameless, valueless generic node is decorative -> falls back to bare root
    assert pruned == {"role": "generic"}


# ---- _cap_long_child_lists (token-budget cap on big tables / long <select>s) ----------------

from agent.perception import _cap_long_child_lists  # noqa: E402


def test_cap_truncates_a_long_data_table_and_leaves_a_marker():
    tree = {"role": "rowgroup", "children": [
        {"role": "row", "name": f"r{i}"} for i in range(25)
    ]}
    _cap_long_child_lists(tree)
    rows = [c for c in tree["children"] if c["role"] == "row"]
    notes = [c for c in tree["children"] if c["role"] == "note"]
    assert len(rows) == 8
    assert len(notes) == 1 and "17 more" in notes[0]["name"]


def test_cap_keeps_many_more_options_than_rows():
    tree = {"role": "combobox", "children": [
        {"role": "option", "name": f"o{i}"} for i in range(30)
    ]}
    _cap_long_child_lists(tree)
    opts = [c for c in tree["children"] if c["role"] == "option"]
    assert len(opts) == 30  # under the 40 option cap -> untouched


def test_cap_is_a_noop_for_a_short_list():
    tree = {"role": "table", "children": [{"role": "row", "name": "only"}]}
    _cap_long_child_lists(tree)
    assert len(tree["children"]) == 1
