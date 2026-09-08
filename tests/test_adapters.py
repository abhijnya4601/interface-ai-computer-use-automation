"""
Conformance tests for the surface-adapter Protocols. These are what make REPORT §4's
"perception and action are the only surface-specific modules" claim enforceable: if either the
web functions or the desktop adapter drift out of the shape the rest of the system depends on,
this fails.
"""
from agent.adapters import ActionExecutor, DesktopAdapter, PerceptionAdapter, WebAdapter


def test_web_adapter_satisfies_both_protocols():
    web = WebAdapter()
    assert isinstance(web, PerceptionAdapter)
    assert isinstance(web, ActionExecutor)


def test_desktop_adapter_satisfies_both_protocols_and_is_real():
    desktop = DesktopAdapter()
    assert isinstance(desktop, PerceptionAdapter)
    assert isinstance(desktop, ActionExecutor)
    # not a stub: observe() actually returns a normalised tree
    from desktop.mock_ax import MockDesktopApp
    obs = desktop.observe(MockDesktopApp())
    assert obs["accessibility_tree"]["role"] == "window"
    assert obs["url"].startswith("window://")


def test_the_shipped_web_functions_have_the_signatures_the_adapter_wraps():
    from agent.perception import build_observation
    from agent.tools import execute_click, execute_extract, execute_navigate, execute_type

    assert callable(build_observation)
    assert callable(execute_click) and callable(execute_type)
    assert callable(execute_navigate) and callable(execute_extract)
