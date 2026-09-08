"""
Surface adapters — the seam that makes "heterogeneous surfaces" (REPORT §4) a checkable claim
instead of a paragraph.

Everything downstream of discovery — the recorder, the artifact schema, the replay engine —
only ever sees two things: a `{role, name, value, children}` accessibility tree, and
role/name-addressed actions. Nothing in that path imports Playwright. These two Protocols write
that contract down explicitly:

  - `PerceptionAdapter.observe(handle)` -> the normalized tree (+ url + last action result)
  - `ActionExecutor.click/type_text/navigate/extract(handle, ...)` -> a short result string

The **web** implementation already exists and is split exactly along this line:
`agent/perception.py:build_observation` is the PerceptionAdapter, `agent/tools.py:execute_*`
is the ActionExecutor, `handle` is a Playwright `Page`. The **desktop** implementation is real,
not a stub: `agent/desktop_adapter.py:DesktopAdapter` runs against `desktop/mock_ax.py` (an
in-process stand-in for Windows UIA / macOS AX) and produces the exact same
`{role, name, value, children}` tree from a non-Playwright surface — nothing else in the system
changes: same `Step`, same 4-tier locator concept, same replay contract, same guardrails.

`runtime_checkable` so a smoke test / CI can assert both the web functions and the desktop
adapter actually satisfy the Protocol (see tests/test_adapters.py), which is what keeps this
honest as either surface's code evolves.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PerceptionAdapter(Protocol):
    """Turn whatever the surface exposes into the normalized observation the rest of the system
    reasons over. `handle` is surface-specific and opaque to callers (a Playwright Page today;
    a UIA/AX root element for desktop)."""

    def observe(self, handle: object, last_action_result: str = "") -> dict:
        """Return `{"url": str, "last_action_result": str, "accessibility_tree": {role, name,
        value, children}}`. Iframe / child-window content must already be merged in — callers
        never reason about frame or window boundaries."""
        ...


@runtime_checkable
class ActionExecutor(Protocol):
    """Execute one role/name-addressed action against the surface and return a short
    human-readable result string (the same string that becomes `last_action_result`)."""

    def click(self, handle: object, role: str, name: str) -> str: ...
    def type_text(self, handle: object, role: str, name: str, text: str) -> str: ...
    def navigate(self, handle: object, url: str) -> str: ...
    def extract(self, handle: object, role: str, name: str) -> str: ...


class WebAdapter:
    """Thin object wrapper over the existing module-level web functions, so the web surface has
    a concrete object that satisfies both Protocols. The functions remain the real
    implementation and the rest of the code keeps calling them directly; this exists for
    dependency injection and for the Protocol conformance test."""

    def observe(self, handle: object, last_action_result: str = "") -> dict:
        from agent.perception import build_observation
        return build_observation(handle, last_action_result)

    def click(self, handle: object, role: str, name: str) -> str:
        from agent.tools import execute_click
        return execute_click(handle, role, name)

    def type_text(self, handle: object, role: str, name: str, text: str) -> str:
        from agent.tools import execute_type
        return execute_type(handle, role, name, text)

    def navigate(self, handle: object, url: str) -> str:
        from agent.tools import execute_navigate
        return execute_navigate(handle, url)

    def extract(self, handle: object, role: str, name: str) -> str:
        from agent.tools import execute_extract
        return execute_extract(handle, role, name)


# The desktop implementation is real and lives in its own module (it pulls in desktop/mock_ax).
# Re-exported here so callers can `from agent.adapters import DesktopAdapter` alongside WebAdapter.
from agent.desktop_adapter import DesktopAdapter

__all__ = ["ActionExecutor", "DesktopAdapter", "PerceptionAdapter", "WebAdapter"]
