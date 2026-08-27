"""
Perception layer — turns whatever Playwright can tell us about the current page into the
small, LLM-friendly observation dict the discovery loop reasons over:

    {"url": ..., "accessibility_tree": {"role": ..., "name": ..., "value": ..., "children": [...]},
     "last_action_result": ...}

Primary signal is the accessibility tree (role/name/value), not a screenshot or raw DOM/CSS —
this is what lets the same agent work against markup that has zero data-testid attributes,
non-semantic class names, and nested-table layouts (see app/templates/*.html): none of that
noise is visible in the accessibility tree, only the semantic role/name/value that a screen
reader (or this agent) would see. Screenshots are reserved for failure evidence, not decision
input — see escalation/controller.py and replay/engine.py's hard-failure path.

IMPORTANT — the build spec calls for
`page.accessibility.snapshot()`, but that API was removed from Playwright (tested directly
against a real browser: `AttributeError: 'Page' object has no attribute 'accessibility'` on
playwright==1.62.0). The supported replacement is `Locator.aria_snapshot()`, which returns a
YAML-formatted tree instead of a nested dict, and — also verified directly, not assumed — does
NOT reach across iframe boundaries when called on the top-level page. Both facts are baked into
the design below:
  - `_parse_aria_snapshot` turns the YAML text into the same {role, name, value, children} shape
    the rest of the system (schema, tests, recorder) was designed around, so nothing downstream
    needs to know the upstream API changed.
  - `build_observation` separately snapshots every child frame (`page.frames[1:]`) and grafts
    each one's tree onto a synthetic `role: "Iframe"` node (capitalized — signals "perception
    stitched this together," since no real ARIA role is capitalized) in place of the leaf
    `iframe` node the top-level snapshot stops at.
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from agent.legacy_locate import CONTROL_ROLES, derive_unnamed_field_labels

_NODE_HEAD_RE = re.compile(r'^(?P<role>[A-Za-z][A-Za-z0-9_-]*)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?')


def _parse_node_head(text: str) -> tuple[str, str | None]:
    """'button \"Go\"' -> ('button', 'Go'); 'iframe' -> ('iframe', None)."""
    match = _NODE_HEAD_RE.match(text.strip())
    if not match:
        return text.strip(), None
    role = match.group("role")
    name = match.group("name")
    if name is not None:
        name = name.replace('\\"', '"')
    return role, name


def _walk_yaml_node(node: Any) -> dict | None:
    """
    Turn one node from yaml.safe_load(aria_snapshot_text) into {role, name?, value?, children?}.
    Returns None for pure-metadata entries (e.g. a link's "/url: ..." child) which aren't part
    of the accessible role/name/value/children contract.
    """
    if isinstance(node, str):
        role, name = _parse_node_head(node)
        out: dict = {"role": role}
        if name:
            out["name"] = name
        return out

    if isinstance(node, dict):
        # aria_snapshot only ever emits single-key dicts per node.
        (key, value), = node.items()
        if key.startswith("/"):
            # metadata line, e.g. {"/url": "/member/12345"} nested under a link — not an
            # accessibility child, drop it. (See REPORT.md Cuts: link targets aren't surfaced
            # to the agent; it navigates by clicking, not by reading hrefs.)
            return None

        role, name = _parse_node_head(key)
        out = {"role": role}
        if name:
            out["name"] = name

        if isinstance(value, str):
            out["value"] = value
        elif isinstance(value, list):
            children = [c for c in (_walk_yaml_node(item) for item in value) if c is not None]
            if children:
                out["children"] = children
        return out

    return None


def _parse_aria_snapshot(snapshot_text: str) -> dict:
    """
    Pure function: parse the YAML text `Locator.aria_snapshot()` returns into a
    {role, name, value, children} tree. Root is always a single node (Playwright's snapshot is
    always one root, typically role "document" or "generic").
    """
    parsed = yaml.safe_load(snapshot_text)
    if not parsed:
        return {"role": "document"}
    # yaml.safe_load of a single top-level "- document:\n  ..." block yields a one-item list.
    root_node = parsed[0] if isinstance(parsed, list) else parsed
    tree = _walk_yaml_node(root_node)
    return tree or {"role": "document"}


# A member record's SHARES/BALANCES table can be ~30 rows and a transfer form's from/to
# <select> ~30 <option>s — repeated in every turn's observation, they dominate token cost while
# adding nothing past the first few (the agent acts on row 0 / picks one option). Cap long
# homogeneous child lists and leave a marker so the agent knows more exist.
# Data-table rows past the first few add tokens without information (the agent acts on row 0).
# <option>s are different: the agent may need to pick one deep in the list by its label, so keep
# many more of those.
_MAX_ROWS = 8
_MAX_OPTIONS = 40
_TRUNCATABLE_CONTAINER_ROLES = ("rowgroup", "table", "list", "listbox", "combobox", "menu")
_TRUNCATABLE_ITEM_ROLES = ("row", "option", "listitem", "menuitem")


def _cap_long_child_lists(node: dict) -> None:
    children = node.get("children")
    if not children:
        return
    if node.get("role") in _TRUNCATABLE_CONTAINER_ROLES:
        items = [c for c in children if c.get("role") in _TRUNCATABLE_ITEM_ROLES]
        limit = _MAX_OPTIONS if (items and items[0].get("role") == "option") else _MAX_ROWS
        if len(items) > limit:
            keep_ids = {id(c) for c in items[:limit]}
            dropped = len(items) - limit
            node["children"] = [
                c for c in children
                if c.get("role") not in _TRUNCATABLE_ITEM_ROLES or id(c) in keep_ids
            ]
            node["children"].append(
                {"role": "note", "name": f"... {dropped} more {items[0]['role']}s not shown "
                                         "(same shape; act on the ones above by position)"}
            )
    for child in node.get("children") or []:
        _cap_long_child_lists(child)


def prune_accessibility_tree(raw_tree: dict, max_depth: int = 15) -> dict:
    """
    Pure function. Keeps only role/name/value/children, truncates depth, drops
    empty/decorative nodes (no name, no value, no children, and a role that carries no
    information on its own — generic wrapper divs the layout is full of), and caps long
    homogeneous child lists (big data tables, long <select>s). Never raises on missing/empty
    `children`.
    """

    def _prune(node: dict, depth: int) -> dict | None:
        role = node.get("role", "generic")
        name = node.get("name")
        value = node.get("value")
        raw_children = node.get("children") or []

        if depth >= max_depth:
            pruned_children: list[dict] = []
        else:
            pruned_children = [
                child for child in (_prune(c, depth + 1) for c in raw_children) if child is not None
            ]

        is_decorative = role in ("generic", "none", "presentation")
        if is_decorative and not name and not value and not pruned_children:
            return None

        out: dict = {"role": role}
        if name:
            out["name"] = name
        if value:
            out["value"] = value
        if pruned_children:
            out["children"] = pruned_children
        return out

    pruned = _prune(raw_tree, depth=0) or {"role": raw_tree.get("role", "generic")}
    _cap_long_child_lists(pruned)
    return pruned


def build_observation(page, last_action_result: str = "") -> dict:
    """
    Requires a live Playwright `page`. Snapshots the main frame via aria_snapshot, then grafts
    in each child frame's own snapshot wherever the main tree has a bare `iframe` leaf, so
    content inside e.g. the sub-account confirmation iframe is reachable by the agent exactly
    as if it weren't cross-document. See module docstring for the full story.
    """
    main_text = page.locator("html").aria_snapshot()
    tree = _parse_aria_snapshot(main_text)

    child_frames = [f for f in page.frames if f != page.main_frame]
    if child_frames:
        iframe_nodes = _find_nodes_by_role(tree, "iframe")
        for iframe_node, frame in zip(iframe_nodes, child_frames):
            try:
                frame_text = frame.locator("html").aria_snapshot()
                frame_tree = _parse_aria_snapshot(frame_text)
            except Exception as exc:  # frame navigated away / detached mid-snapshot
                iframe_node["role"] = "Iframe"
                iframe_node["value"] = f"<content unavailable: {exc}>"
                continue
            iframe_node["role"] = "Iframe"
            iframe_node["name"] = frame.url
            if frame_tree.get("children"):
                iframe_node["children"] = frame_tree["children"]
            elif frame_tree.get("name") or frame_tree.get("value"):
                iframe_node["children"] = [frame_tree]

    try:
        _enrich_unnamed_controls(page, tree)
    except Exception:
        # Enrichment is a best-effort adapter for label-less legacy forms — a page that can't be
        # evaluated (mid-navigation, detached frame) must never break the core observation.
        pass

    pruned = prune_accessibility_tree(tree)
    return {
        "url": page.url,
        "accessibility_tree": pruned,
        "last_action_result": last_action_result,
    }


def _collect_nameless_control_nodes(node: dict, out: list[dict] | None = None) -> list[dict]:
    """Every interactive control node in document order that the accessibility tree left with no
    name — the ones an LLM can see but can't target."""
    out = [] if out is None else out
    if node.get("role") in CONTROL_ROLES and not node.get("name"):
        out.append(node)
    for child in node.get("children") or []:
        _collect_nameless_control_nodes(child, out)
    return out


def _enrich_unnamed_controls(page, tree: dict) -> None:
    """
    MERIDIAN CORE and surfaces like it give form controls no accessible name at all (no
    ``<label for>``, no ``aria-label``, no placeholder). The raw accessibility tree then shows a
    bare ``- textbox`` the LLM can describe but not act on. This pass derives a name for each
    such control from the visible label text next to it (see agent/legacy_locate.py), matching
    the derived list against the tree's nameless control nodes in document order — the order
    ``aria_snapshot()`` and ``querySelectorAll`` agree on. Mutates ``tree`` in place. If the two
    lists don't line up (some controls did have names after all), only the aligned prefix is
    enriched — a wrong name is worse than none.
    """
    fields: list[dict] = list(derive_unnamed_field_labels(page))
    for frame in page.frames:
        if frame != page.main_frame:
            fields.extend(derive_unnamed_field_labels(frame))
    if not fields:
        return
    nameless = _collect_nameless_control_nodes(tree)
    for node, field in zip(nameless, fields):
        field = field or {}
        if field.get("label"):
            node["name"] = field["label"]
        # carry the control's current value too — MERIDIAN's <select>s and pre-filled inputs
        # otherwise look unchanged in the pruned tree, which makes filling a form read as a
        # dead-end and hides from the model what it has already entered.
        if field.get("value"):
            node["value"] = field["value"]


def _find_nodes_by_role(node: dict, role: str) -> list[dict]:
    found = []
    if node.get("role") == role:
        found.append(node)
    for child in node.get("children") or []:
        found.extend(_find_nodes_by_role(child, role))
    return found
