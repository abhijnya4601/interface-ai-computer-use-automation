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
   check instead of an exact match against the extracted ID — that produced a real bug (see
   DECISIONS.md D13): recording `open_subaccount` for the goal "...member 12345 with a $50
   opening deposit...", the deposit amount "50" is *also* a substring of the goal (inside
   "$50"), so it got tagged `{"param_ref": "member_id"}` too. Replaying with a different
   member_id would then have typed the member_id into the deposit field. Matching only the
   goal's actual extracted ID, exactly, closes that hole.

3. **`table_position` locator, for cells with no per-row label** (D22, fixing D21's real find):
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

    # ---- table_position locator (D22) -------------------------------------------------------

    def _try_table_position_locator(self, role: str, name: str, page) -> LocatorTarget | None:
        """
        If (role, name) resolves to a single <td> cell sitting in a data-table row with no
        per-row label (<th>), but the table itself has column headers (<th scope="col">), build
        a position-based locator instead of the normal role_name-by-value tier — see module
        docstring point 3 / DECISIONS.md D22. Returns None (caller falls back to the normal
        tiers) if the shape doesn't match; never raises.
        """
        if role.lower() != "cell":
            return None

        contexts = [page] + [f for f in page.frames if f != page.main_frame]
        for ctx in contexts:
            try:
                candidate = ctx.get_by_role("cell", name=name)
                if candidate.count() != 1:
                    continue
            except Exception:
                continue

            cell = candidate.first
            try:
                row = cell.locator("xpath=ancestor::tr[1]")
                if row.count() == 0 or row.locator("xpath=./th").count() > 0:
                    continue  # has its own label -- the label/value tiers already cover this

                table = cell.locator("xpath=ancestor::table[1]")
                if table.count() == 0:
                    continue
                header_row = table.locator("xpath=.//tr[th[@scope='col']]").first
                if header_row.count() == 0:
                    continue
                headers = header_row.locator("th").all_text_contents()
                if not headers:
                    continue

                row_index = row.evaluate(
                    "el => Array.from(el.parentElement.children)"
                    ".filter(tr => tr.querySelector('td'))"
                    ".indexOf(el)"
                )
                col_index = cell.evaluate("el => Array.from(el.parentElement.children).indexOf(el)")
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
                        "moment the underlying data changes (see DECISIONS.md D21/D22), since "
                        "that value is exactly what's different on every replay. Addressed by "
                        f"position instead: row {row_index} (0-indexed among data rows), column "
                        f"{col_index} ({column_label!r})."
                    ),
                )
            except Exception:
                continue
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
        if table_target is not None:
            target = table_target
            self.tier_log.append({"step_id": step_id, "role": role, "name": name, "tier": "table_position"})
        else:
            target = self.build_locator(role, name, page, step_id)
        step = Step(step_id=step_id, action_type="extract", target=target, extract_as=as_var)
        self.steps.append(step)
        return step
