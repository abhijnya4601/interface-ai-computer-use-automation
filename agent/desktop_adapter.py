"""
A REAL implementation of the perception + action Protocols (agent/adapters.py) for a desktop
surface — not the stub `DesktopAdapter` that used to live in adapters.py. It runs against
`desktop/mock_ax.py`, an in-process stand-in for an OS accessibility API, which is enough to
prove the load-bearing claim: perception normalisation and role/name-addressed actions work
unchanged on a non-Playwright surface, so `Step` / `Capability` / the replay contract are
genuinely surface-agnostic.

A production version swaps `desktop/mock_ax.py` for `pywinauto` / UIAutomation (Windows) or
`pyobjc` + AXUIElement (macOS). `observe()` still emits the same `{role, name, value, children}`
tree; the `execute_*` methods still call Invoke / SetValue / window activation. Nothing in
recorder / schema / replay changes.
"""
from __future__ import annotations

from agent.tools import ToolExecutionError
from desktop.mock_ax import AXElement, MockDesktopApp


def _normalise(el: AXElement) -> dict:
    """AX element -> the same {role, name, value, children} shape agent/perception.py produces
    from aria_snapshot() YAML. This is the whole point of the seam."""
    node: dict = {"role": el.role.lower(), "name": el.name, "value": el.value}
    kids = [_normalise(c) for c in el.children]
    if kids:
        node["children"] = kids
    return node


class DesktopAdapter:
    """`handle` is a `MockDesktopApp` (a real one would wrap the OS accessibility client)."""

    def observe(self, handle: MockDesktopApp, last_action_result: str = "") -> dict:
        return {
            "url": f"window://{handle.window}",   # the desktop analogue of a URL
            "last_action_result": last_action_result,
            "accessibility_tree": _normalise(handle.root()),
        }

    # ---- actions, role/name-addressed exactly like the web executor -------------------------

    def _require(self, handle: MockDesktopApp, role: str, name: str) -> AXElement:
        el = handle.root().find(role, name)
        if el is None:
            raise ToolExecutionError(f"no {role} named {name!r} in the {handle.window} window")
        return el

    def click(self, handle: MockDesktopApp, role: str, name: str) -> str:
        try:
            return handle.invoke(self._require(handle, role, name))
        except RuntimeError as exc:
            raise ToolExecutionError(str(exc)) from exc

    def type_text(self, handle: MockDesktopApp, role: str, name: str, text: str) -> str:
        try:
            return handle.set_value(self._require(handle, role, name), text)
        except RuntimeError as exc:
            raise ToolExecutionError(str(exc)) from exc

    def navigate(self, handle: MockDesktopApp, url: str) -> str:
        # "navigate" on a desktop surface = activate a window/screen; url is "window://<name>"
        window = url.split("://", 1)[-1] if "://" in url else url
        try:
            return handle.activate(window)
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc

    def extract(self, handle: MockDesktopApp, role: str, name: str) -> str:
        el = self._require(handle, role, name)
        if el.value:
            return el.value
        # a labelled row: value is the first child StaticText's value (mirrors the web
        # executor walking to the sibling <td>)
        for child in el.children:
            if child.value:
                return child.value
        return (el.value or "").strip()
