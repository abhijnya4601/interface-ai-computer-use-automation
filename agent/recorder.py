"""
Recorder — runs alongside the discovery loop (called from discovery.py right after each tool
call is accepted and executed, not as a post-hoc pass over a transcript) and turns each action
into an artifact/schema.py `Step`, accumulating `list[Step]` for the compiler to turn into a
`Capability` once the run finishes.

Two things worth understanding about the design:

1. **3-tier locator fallback**, always attempted in this order and logged (`self.tier_log`):
   tier 1 (`role_name`) if role+name resolves to exactly one element anywhere on the page/in its
   frames; tier 2 (`structural`) if it resolves to more than one (falls back to "the first match,
   in DOM order" — a real position-relative-to-anchor description, e.g. "2nd row of results
   table," would need more page-structure context than a single tool call carries, so this is a
   deliberately simple version of tier 2, documented as a cut in REPORT.md); tier 3 (`text`) if
   role+name resolves to nothing. `tier_log` is also the drift-detection signal the assignment
   asks for: if a capability starts needing tier 2/3 more often across successive replays, that's
   a free signal the underlying UI has drifted, with zero extra infrastructure.

2. **Parameter detection is a deliberate scope cut**, not an oversight: the member ID is
   extracted from the goal once via a fixed pattern (`member <digits>`), and a typed/extracted
   value is tagged `{"param_ref": "member_id"}` only if it *exactly equals* that extracted
   value. This only works because both of this project's capabilities have exactly one varying
   input (a member ID) — a general system would need either the LLM to name its own parameters
   or a real slot-filling NLP step. Called out explicitly in REPORT.md's Cuts section, not
   silently limited.

   Earlier this used a blind "does this literal appear anywhere in the goal string" substring
   check instead of an exact match against the extracted ID — that produced a real bug: recording
   `open_subaccount` for the goal "...member 12345 with a $50
   opening deposit...", the deposit amount "50" is *also* a substring of the goal (inside
   "$50"), so it got tagged `{"param_ref": "member_id"}` too. Replaying with a different
   member_id would then have typed the member_id into the deposit field. Matching only the
   goal's actual extracted ID, exactly, closes that hole.

3. **`table_position` locator, for cells with no per-row label**:
   `extract`ing a labeled value (`<th scope="row">Savings Balance</th><td>$1,842.30</td>`)
   anchors on the label — stable, since the label doesn't depend on the data. A plain data-table
   row (`<td>2026-08-15</td><td>Grocery Store Purchase</td>...`, no per-row label) has nothing
   like that to anchor on; the only thing distinguishing "the date cell" from any other cell was
   its own value, which is exactly what's different on every replay. `_try_table_position_locator`
   detects this shape (a `<td>` inside a table whose row has no `<th>`, but the table itself has
   `<th scope="col">` column headers) and addresses the cell by position instead — which table
   (identified by its column headers, since those don't depend on data), which row, which column.
"""
from __future__ import annotations

import re

from agent.legacy_locate import (
    locate_field_name,
    locate_labeled_field,
    locate_labeled_value,
    normalize_label,
)
from artifact.schema import LocatorTarget, Step

_PARAM_NAME = "member_id"  # see module docstring — the only varying input across both capabilities
_MEMBER_ID_RE = re.compile(r"member\s+(\d+)", re.IGNORECASE)


class Recorder:
    def __init__(self, goal: str):
        self.goal = goal
        self.steps: list[Step] = []
        self.tier_log: list[dict] = []
        self._counter = 0
        match = _MEMBER_ID_RE.search(goal)
        self._member_id_value = match.group(1) if match else None

    def _next_step_id(self) -> str:
        self._counter += 1
        return f"s{self._counter}"

    # ---- 3-tier locator builder -----------------------------------------------------------

    def build_locator(self, role: str, name: str, page, step_id: str) -> LocatorTarget:
        role_norm = role.lower()
        contexts = [page] + [f for f in page.frames if f != page.main_frame]
        total = 0
        for ctx in contexts:
            try:
                total += ctx.get_by_role(role_norm, name=name).count()
            except Exception:
                continue

        if total == 1:
            tier = "role_name"
            target = LocatorTarget(
                strategy="role_name",
                primary={"role": role_norm, "name": name},
                fallbacks=[{"strategy": "text", "text": name}],
                reasoning=(
                    f"role={role_norm!r} name={name!r} resolves to exactly one element across "
                    "the page and its frames. Backed by real semantic HTML (a real <button>, "
                    "<label for>, or <th scope=row> — see app/templates), not any CSS class or "
                    "test ID, so it survives markup/styling churn and only breaks if the visible "
                    "label text or the element's semantic role itself changes."
                ),
            )
        elif total > 1:
            tier = "structural"
            target = LocatorTarget(
                strategy="structural",
                primary={"role": role_norm, "name": name, "nth": 0},
                fallbacks=[{"strategy": "text", "text": name}],
                reasoning=(
                    f"role={role_norm!r} name={name!r} matched {total} elements — not unique. "
                    "Resolved structurally as the first (index 0) match in DOM order, since that "
                    "is what the discovery agent actually acted on. Weaker than tier 1: only "
                    "reliable if replay's runtime page produces matches in the same order."
                ),
            )
        else:
            legacy = self._try_legacy_field_locator(role_norm, name, page, step_id)
            if legacy is not None:
                return legacy
            tier = "text"
            target = LocatorTarget(
                strategy="text",
                primary={"text": name},
                fallbacks=[],
                reasoning=(
                    f"no element matched role={role_norm!r} name={name!r} via the accessibility "
                    "tree at record time; falling back to a raw text-content match. This is the "
                    "most brittle tier — it breaks on any copy change — and is logged as a "
                    "warning below for exactly that reason."
                ),
            )

        self.tier_log.append({"step_id": step_id, "role": role_norm, "name": name, "tier": tier})
        if tier == "text":
            print(f"[recorder] WARNING: step {step_id} ({role_norm} '{name}') fell back to "
                  "tier-3 text locator — most brittle, watch this in future replays")
        return target

    # ---- legacy label-less form field locator ---------------------------------------------

    def _try_legacy_field_locator(self, role: str, name: str, page, step_id: str) -> LocatorTarget | None:
        """
        The accessibility tree gave role+name zero matches. Before falling back to a raw
        text-content match, try the legacy-web strategies (agent/legacy_locate.py): resolve the
        control by the visible label next to it, and — if that lands a single element — read its
        server-contract ``name=`` attribute off the page and record it as a fallback tier.
        MERIDIAN CORE's form controls have no accessible name at all, so this is the tier that
        actually carries login, search, transfer, hold, and update. Returns None (caller falls
        back to ``text``) if the label doesn't resolve either.
        """
        contexts = [page] + [f for f in page.frames if f != page.main_frame]
        for ctx in contexts:
            try:
                loc = locate_labeled_field(ctx, name, control_role=role)
            except Exception:
                continue
            if loc is None:
                continue
            try:
                if loc.count() != 1:
                    continue
                name_attr = loc.get_attribute("name")
            except Exception:
                continue

            fallbacks: list[dict] = []
            if name_attr:
                fallbacks.append({"strategy": "field_name", "name": name_attr})
            fallbacks.append({"strategy": "text", "text": name})

            self.tier_log.append(
                {"step_id": step_id, "role": role, "name": name, "tier": "labeled_field"}
            )
            return LocatorTarget(
                strategy="labeled_field",
                primary={"label": name, "control_role": role},
                fallbacks=fallbacks,
                reasoning=(
                    f"role={role!r} name={name!r} resolved to no element via the accessibility "
                    "tree — this legacy surface gives its form controls no accessible name "
                    "(no <label for>, aria-label, or placeholder). Resolved instead by the "
                    f"visible label text {name!r} sitting next to the control, which is what a "
                    "human reads on screen and does not depend on any class or id. Fallback tier "
                    + (f"is the control's server-contract name attribute {name_attr!r} "
                       "(load-bearing HTML the backend requires, not a test id)."
                       if name_attr else "is a raw text-content match.")
                ),
            )
        return None

    # ---- table_position locator -------------------------------------------------------------

    def _try_table_position_locator(self, role: str, name: str, page) -> LocatorTarget | None:
        """
        If (role, name) points at a <td> cell in a data-table row with no per-row label (<th>),
        build a position-based locator instead of anchoring on the cell's own value — which is
        exactly what differs between replays. The header row is a `<th scope="col">` row if the
        table has one, otherwise the table's first row (MERIDIAN CORE's tables label their
        columns with plain `<td>` in a `class="lbl"` first row, not `<th>`). Resolves the target
        cell even when role+name matches many cells (a share-id prefix like `100234-S0001` is a
        substring of `100234-S0001-3`, `-6`, …): the first match is row 0, which is what the
        agent acted on. Returns None (caller falls back to the normal tiers) if the shape
        doesn't fit; never raises.
        """
        if role.lower() != "cell":
            return None

        # Match a LEAF <td> whose own trimmed text is (or contains) the value. get_by_role("cell")
        # also matches the big layout <td> that merely *contains* this text as a descendant —
        # and that ancestor comes first in document order, which is how a MERIDIAN record page
        # ends up reporting the page banner as the "table headers".
        name_lit = name.replace('"', "").strip()
        contexts = [page] + [f for f in page.frames if f != page.main_frame]
        for ctx in contexts:
            cell = None
            for xp in (
                f'xpath=.//td[not(.//td) and normalize-space(.)="{name_lit}"]',
                f'xpath=.//td[not(.//td) and contains(normalize-space(.), "{name_lit}")]',
            ):
                try:
                    cand = ctx.locator(xp)
                    if cand.count() >= 1:
                        cell = cand.first
                        break
                except Exception:
                    continue
            if cell is None:
                continue

            try:
                row = cell.locator("xpath=ancestor::tr[1]")
                if row.count() == 0 or row.locator("xpath=./th").count() > 0:
                    continue  # has its own row label -- the label/value tiers already cover this

                pos = cell.evaluate(
                    """el => {
                        const table = el.closest('table');
                        if (!table) return null;
                        const rows = Array.from(table.querySelectorAll('tr'))
                            .filter(r => r.closest('table') === table);
                        const headerRow =
                            rows.find(r => r.querySelector('th[scope="col"]')) || rows[0];
                        const headerCells = headerRow
                            ? Array.from(headerRow.querySelectorAll('th, td')) : [];
                        const headers = headerCells.map(c => c.textContent.trim());
                        const tr = el.closest('tr');
                        const dataRows = rows.filter(
                            r => r !== headerRow && r.querySelector('td'));
                        return {
                            headers,
                            row_index: dataRows.indexOf(tr),
                            column_index: Array.from(tr.children).indexOf(el),
                        };
                    }"""
                )
                if not pos:
                    continue
                headers = [h for h in (pos.get("headers") or [])]
                row_index, col_index = pos.get("row_index"), pos.get("column_index")
                if not headers or not any(headers):
                    continue
                if row_index is None or row_index < 0 or col_index is None or col_index < 0:
                    continue

                column_label = headers[col_index] if col_index < len(headers) else "?"
                return LocatorTarget(
                    strategy="table_position",
                    primary={"table_headers": headers, "row_index": row_index, "column_index": col_index},
                    fallbacks=[{"strategy": "text", "text": name}],
                    reasoning=(
                        f"role='cell' name={name!r} sits in a data table (columns {headers}) with "
                        "no per-row label — anchoring on the cell's own value would break the "
                        "moment the underlying data changes, since "
                        "that value is exactly what's different on every replay. Addressed by "
                        f"position instead: row {row_index} (0-indexed among data rows), column "
                        f"{col_index} ({column_label!r})."
                    ),
                )
            except Exception:
                continue
        return None

    # ---- labeled_value locator (read-only <td class="lbl">Label:</td><td>Value</td>) --------

    def _try_labeled_value_locator(self, role: str, name: str, page, step_id: str = "?") -> LocatorTarget | None:
        """
        MERIDIAN CORE shows read-only values as ``<td class="lbl">Confirmation:</td><td>
        CN480423</td>`` (member contact fields, transfer/hold confirmations, review screens).
        The stable anchor is the label, never the value. `name` may already be the label
        (the agent extracted by "Confirmation:") or the value itself — try it as a label first,
        then find the label of the row the value sits in. Returns None if the shape doesn't fit.
        """
        if role.lower() not in ("cell", "gridcell", "rowheader"):
            return None
        contexts = [page] + [f for f in page.frames if f != page.main_frame]
        for ctx in contexts:
            label = None
            # (a) name is the label
            try:
                if locate_labeled_value(ctx, name) is not None:
                    label = normalize_label(name)
            except Exception:
                pass
            # (b) name is the value — find its row's label cell
            if label is None:
                nlit = name.replace('"', "").strip()
                try:
                    lbl_cell = ctx.locator(
                        f'xpath=.//td[not(.//td) and normalize-space(.)="{nlit}"]'
                        f'/parent::tr/*[(self::td or self::th) and '
                        f'contains(concat(" ", normalize-space(@class), " "), " lbl ")][1]'
                    )
                    if lbl_cell.count() == 1:
                        cand_label = normalize_label(lbl_cell.first.text_content() or "")
                        if cand_label and locate_labeled_value(ctx, cand_label) is not None:
                            label = cand_label
                except Exception:
                    pass
            if label:
                self.tier_log.append(
                    {"step_id": step_id, "role": role, "name": name, "tier": "labeled_value"}
                )
                return LocatorTarget(
                    strategy="labeled_value",
                    primary={"label": label},
                    fallbacks=[{"strategy": "text", "text": name}],
                    reasoning=(
                        f"read-only value in a <td class='lbl'>{label}:</td><td>…</td> row. "
                        "Anchored on the label text, which is stable, rather than the value, "
                        "which is exactly what differs on every run."
                    ),
                )
        return None

    # ---- parameter detection ---------------------------------------------------------------

    def _maybe_param_ref(self, value: str) -> dict | str:
        if self._member_id_value and str(value) == self._member_id_value:
            return {"param_ref": _PARAM_NAME}
        return value

    # ---- recording one Step per accepted tool call -----------------------------------------

    def record_navigate(self, url: str) -> Step:
        step = Step(step_id=self._next_step_id(), action_type="navigate", value=url)
        self.steps.append(step)
        return step

    def record_click(self, role: str, name: str, page) -> Step:
        step_id = self._next_step_id()
        target = self.build_locator(role, name, page, step_id)
        step = Step(step_id=step_id, action_type="click", target=target)
        self.steps.append(step)
        return step

    def record_type(self, role: str, name: str, text: str, page) -> Step:
        step_id = self._next_step_id()
        target = self.build_locator(role, name, page, step_id)
        step = Step(
            step_id=step_id, action_type="type", target=target, value=self._maybe_param_ref(text)
        )
        self.steps.append(step)
        return step

    def record_extract(self, role: str, name: str, as_var: str, page) -> Step:
        step_id = self._next_step_id()
        table_target = self._try_table_position_locator(role, name, page)
        labeled_value_target = None if table_target is not None else self._try_labeled_value_locator(role, name, page, step_id)
        if table_target is not None:
            target = table_target
            self.tier_log.append({"step_id": step_id, "role": role, "name": name, "tier": "table_position"})
        elif labeled_value_target is not None:
            target = labeled_value_target
        else:
            target = self.build_locator(role, name, page, step_id)
        step = Step(step_id=step_id, action_type="extract", target=target, extract_as=as_var)
        self.steps.append(step)
        return step
