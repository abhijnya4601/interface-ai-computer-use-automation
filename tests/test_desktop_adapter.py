"""
Drive a full discovery-like sequence and a replay-like extraction against the mock desktop
(AX) surface. If this passes, the perception normalisation + role/name-addressed action
contract genuinely holds for a non-Playwright surface - which is REPORT §4's whole claim.
"""
from agent.desktop_adapter import DesktopAdapter
from desktop.mock_ax import MockDesktopApp


def _names(tree, out=None):
    out = out if out is not None else []
    out.append((tree["role"], tree["name"]))
    for c in tree.get("children", []):
        _names(c, out)
    return out


def test_observe_produces_the_same_tree_shape_as_the_web_perception():
    obs = DesktopAdapter().observe(MockDesktopApp())
    node = obs["accessibility_tree"]
    assert set(node) >= {"role", "name", "value"}
    assert ("textfield", "Search") in _names(node)
    assert ("button", "Go") in _names(node)


def test_happy_path_lookup_by_role_and_name_only():
    app = MockDesktopApp()
    d = DesktopAdapter()

    d.type_text(app, "TextField", "Search", "23456")
    d.click(app, "Button", "Go")
    obs = d.observe(app, "clicked Go")
    assert obs["url"] == "window://member"
    assert ("row", "Savings Balance") in _names(obs["accessibility_tree"])

    value = d.extract(app, "Row", "Savings Balance")
    assert value == "$5.02"


def test_not_found_branch_is_observable_the_same_way_the_web_one_is():
    app = MockDesktopApp()
    d = DesktopAdapter()
    d.type_text(app, "TextField", "Search", "00000")
    d.click(app, "Button", "Go")
    obs = d.observe(app)
    assert ("statictext", "No results.") in _names(obs["accessibility_tree"])


def test_locked_account_branch():
    app = MockDesktopApp()
    d = DesktopAdapter()
    d.navigate(app, "window://member:99999")  # jump straight to the member window
    obs = d.observe(app)
    labels = [n for _, n in _names(obs["accessibility_tree"])]
    assert any("Access denied" in x for x in labels)


def test_empty_balance_cell_extracts_as_empty_not_as_the_label():
    app = MockDesktopApp()
    d = DesktopAdapter()
    d.type_text(app, "TextField", "Search", "77777")
    d.click(app, "Button", "Go")
    assert d.extract(app, "Row", "Savings Balance") == ""


def test_acting_on_a_missing_element_raises_toolexecutionerror():
    import pytest

    from agent.tools import ToolExecutionError

    d = DesktopAdapter()
    with pytest.raises(ToolExecutionError):
        d.click(MockDesktopApp(), "Button", "Nonexistent")
