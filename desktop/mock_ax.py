"""
A tiny in-process stand-in for an OS accessibility API (Windows UIA / macOS AX): a control
tree of {role, name, value, children} elements with `Invoke` / `SetValue` actions and window
navigation. It exists so the desktop adapter (`agent/desktop_adapter.py`) can be exercised for
real — proving the perception/action seam holds for a non-Playwright surface — without OS
accessibility permissions or a real native app.

The modelled app mirrors the mock bank at a small scale: a Search window (a text field + a Go
button + a results list) and a Member window (labelled fields incl. a balance). Same
role/name-addressed interaction the web surface uses.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AXElement:
    role: str
    name: str = ""
    value: str = ""
    children: list[AXElement] = field(default_factory=list)
    actions: tuple[str, ...] = ()  # e.g. ("Invoke",) for a button, ("SetValue",) for a field

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def find(self, role: str, name: str) -> AXElement | None:
        role, name = role.lower(), name
        for el in self.walk():
            if el.role.lower() == role and el.name == name:
                return el
        return None


# --- the modelled application --------------------------------------------------------------

_MEMBERS = {
    "12345": {"name": "Dana Whitfield", "balance": "$1,842.30", "status": "active"},
    "23456": {"name": "Marcus Oyelaran", "balance": "$5.02", "status": "active"},
    "77777": {"name": "Nadia Farouk", "balance": "", "status": "active"},   # ledger returned nothing
    "99999": {"name": "Restricted Account", "balance": "", "status": "locked"},
}


class MockDesktopApp:
    """Holds the current window and mutable field state. `root()` returns the AX tree for the
    window currently shown."""

    def __init__(self) -> None:
        self.window = "search"
        self._query = ""
        self._member_id: str | None = None

    # navigation is 'open this window/screen' — the desktop analogue of a URL
    def activate(self, window: str) -> str:
        if window == "search":
            self.window = "search"
            self._member_id = None
            return "opened Search window"
        if window.startswith("member:"):
            self._member_id = window.split(":", 1)[1]
            self.window = "member"
            return f"opened Member window for {self._member_id}"
        raise ValueError(f"no such window {window!r}")

    def set_value(self, el: AXElement, text: str) -> str:
        if "SetValue" not in el.actions:
            raise RuntimeError(f"{el.role} {el.name!r} is not editable")
        if el.name == "Search":
            self._query = text
        el.value = text
        return f"set {el.name!r} to {text!r}"

    def invoke(self, el: AXElement) -> str:
        if "Invoke" not in el.actions:
            raise RuntimeError(f"{el.role} {el.name!r} is not invokable")
        if el.name == "Go":
            return self.activate(f"member:{self._query}") if self._query in _MEMBERS \
                else self._activate_no_results()
        if el.name == "View":
            return self.activate(f"member:{self._query}")
        raise RuntimeError(f"nothing wired for {el.name!r}")

    def _activate_no_results(self) -> str:
        self.window = "search"
        self._member_id = "__none__"
        return "no results"

    def root(self) -> AXElement:
        if self.window == "search":
            kids = [
                AXElement("TextField", "Search", self._query, actions=("SetValue",)),
                AXElement("Button", "Go", actions=("Invoke",)),
            ]
            if self._member_id == "__none__":
                kids.append(AXElement("StaticText", "No results."))
            elif self._member_id is None and self._query in _MEMBERS:
                kids.append(AXElement("Button", "View", actions=("Invoke",)))
            return AXElement("Window", "Search", children=kids)

        m = _MEMBERS.get(self._member_id or "")
        if not m:
            return AXElement("Window", "Member", children=[
                AXElement("StaticText", "No member record found"),
            ])
        if m["status"] == "locked":
            return AXElement("Window", "Member", children=[
                AXElement("StaticText", "Access denied. This account is restricted"),
            ])
        return AXElement("Window", "Member", children=[
            AXElement("Row", "Name", children=[AXElement("StaticText", "", m["name"])]),
            AXElement("Row", "Savings Balance",
                      children=[AXElement("StaticText", "", m["balance"])]),
        ])
