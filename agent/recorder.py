"""
Recorder - runs alongside the discovery loop (called from discovery.py right after each tool
call is accepted and executed, not as a post-hoc pass over a transcript) and turns each action
into an artifact/schema.py `Step`, accumulating `list[Step]` for the compiler to turn into a
`Capability` once the run finishes.

Two things worth understanding about the design:

1. **3-tier locator fallback**, always attempted in this order and logged (`self.tier_log`):
   tier 1 (`role_name`) if role+name resolves to exactly one element anywhere on the page/in its
   frames; tier 2 (`structural`) if it resolves to more than one (falls back to "the first match,
   in DOM order" - a real position-relative-to-anchor description, e.g. "2nd row of results
   table," would need more page-structure context than a single tool call carries, so this is a
   deliberately simple version of tier 2, documented as a cut in REPORT.md); tier 3 (`text`) if
   role+name resolves to nothing. `tier_log` is also the drift-detection signal the assignment
   asks for: if a capability starts needing tier 2/3 more often across successive replays, that's
   a free signal the underlying UI has drifted, with zero extra infrastructure.

2. **Parameter naming is the agent's job now**, not a regex: the `type` and `navigate` tools
   take an optional `param_name`, and when the agent sets it the recorder stores
   `{"param_ref": param_name}` (plus a `url_template` for a partly-parameterized navigation),
   which flows straight into `Capability.input_schema` at compile time. This is the general
   slot-filler - any number of inputs, any names, chosen by the model that just used the value.

   The old fixed `member <digits>` goal-text regex (`_maybe_param_ref`) is kept as a *fallback*
   for a model that forgets to tag a value - it still only fires on an *exact* match against
   the ID pulled from the goal. That exactness matters: an earlier blind-substring version
   tagged a "$50" deposit as `member_id` because "50" appeared in the goal text, and replaying
   with a different member_id would have typed it into the deposit field.

3. **`table_position` locator, for cells with no per-row label**:
   `extract`ing a labeled value (`<th scope="row">Savings Balance</th><td>$1,842.30</td>`)
   anchors on the label - stable, since the label doesn't depend on the data. A plain data-table
   row (`<td>2026-08-15</td><td>Grocery Store Purchase</td>...`, no per-row label) has nothing
   like that to anchor on; the only thing distinguishing "the date cell" from any other cell was
   its own value, which is exactly what's different on every replay. `_try_table_position_locator`
   detects this shape (a `<td>` inside a table whose row has no `<th>`, but the table itself has
   `<th scope="col">` column headers) and addresses the cell by position instead - which table
   (identified by its column headers, since those don't depend on data), which row, which column.
"""
from __future__ import annotations

import re

from artifact.schema import LocatorTarget, Step

_PARAM_NAME = "member_id"  # see module docstring - the only varying input across both capabilities
_MEMBER_ID_RE = re.compile(r"member\s+(\d+)", re.IGNORECASE)


class Recorder:
    def __init__(self, goal: str):
        self.goal = goal
        self.steps: list[Step] = []
        self.tier_log: list[dict] = []
        # Branches / data shapes the discovery agent proposes as it explores (via the
        # note_branch / note_data_shape tools). The compiler attaches these to the artifact as
        # `provenance="proposed"`; a reviewer promotes the good ones into app_knowledge/*.yaml.
        # This is what makes domain knowledge come FROM discovery instead of a hardcoded dict.
        self.proposed_outcomes: list[dict] = []
        self.proposed_contracts: list[dict] = []
        self._counter = 0
        match = _MEMBER_ID_RE.search(goal)
        self._member_id_value = match.group(1) if match else None

    # ---- agent-proposed domain knowledge -------------------------------------------------

    def note_branch(
        self,
        condition: str,
        classification: str = "business_outcome",
        code: str | None = None,
        handling: str | None = None,
        on_role: str | None = None,
        on_name: str | None = None,
        on_action_type: str | None = None,
    ) -> None:
        """Record a branch the agent saw or inferred (e.g. 'if the member is locked, this link
        isn't rendered and the page shows Access denied'). `on_*` pin it to the step it applies
        to; omitted means "any step that could land on this condition"."""
        self.proposed_outcomes.append({
            "condition": condition,
            "classification": classification,
            "code": code,
            "handling": handling,
            "role": on_role,
            "name": on_name,
            "action_type": on_action_type,
        })

    def note_data_shape(
        self,
        extract_as: str,
        pattern: str | None = None,
        placeholders: list[str] | None = None,
        reason: str = "",
    ) -> None:
        """Record what a real value for an extracted field looks like, so replay can tell a
        genuine datum from an empty / placeholder cell."""
        self.proposed_contracts.append({
            "extract_as": extract_as,
            "pattern": pattern,
            "placeholders": placeholders or [],
            "reason": reason,
        })

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
                    "<label for>, or <th scope=row> - see app/templates), not any CSS class or "
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
                    f"role={role_norm!r} name={name!r} matched {total} elements - not unique. "
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
                    "most brittle tier - it breaks on any copy change - and is logged as a "
                    "warning below for exactly that reason."
                ),
            )

        self.tier_log.append({"step_id": step_id, "role": role_norm, "name": name, "tier": tier})
        if tier == "text":
            print(f"[recorder] WARNING: step {step_id} ({role_norm} '{name}') fell back to "
                  "tier-3 text locator - most brittle, watch this in future replays")
        return target

    # ---- table_position locator -------------------------------------------------------------

    def _try_table_position_locator(self, role: str, name: str, page) -> LocatorTarget | None:
        """
        If (role, name) resolves to a single <td> cell sitting in a data-table row with no
        per-row label (<th>), but the table itself has column headers (<th scope="col">), build
        a position-based locator instead of the normal role_name-by-value tier - see module
        docstring point 3. Returns None (caller falls back to the normal
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
                        "no per-row label - anchoring on the cell's own value would break the "
                        "moment the underlying data changes, since "
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
        """Fallback slot-filler: tag a typed value as `{"param_ref": "member_id"}` iff it
        exactly equals the ID pulled from the goal text. Only used when the discovery agent
        did NOT name the parameter itself (`param_name` on the `type` tool) - that explicit
        signal always wins, so this regex is a safety net for a model that forgets to tag."""
        if self._member_id_value and str(value) == self._member_id_value:
            return {"param_ref": _PARAM_NAME}
        return value

    @staticmethod
    def _param_value(param_name: str, url_template: str | None = None) -> dict:
        ref: dict = {"param_ref": param_name}
        if url_template:
            ref["url_template"] = url_template
        return ref

    # ---- recording one Step per accepted tool call -----------------------------------------

    def record_navigate(
        self, url: str, param_name: str | None = None, url_template: str | None = None
    ) -> Step:
        """`param_name` (from the agent) marks this navigation as parameterized. If the whole
        URL is the parameter, `url_template` is omitted; if only part of it varies, pass a
        `"http://host/member/{member_id}/x"`-style template that replay fills in."""
        value = self._param_value(param_name, url_template) if param_name else url
        step = Step(step_id=self._next_step_id(), action_type="navigate", value=value)
        self.steps.append(step)
        return step

    def record_click(self, role: str, name: str, page) -> Step:
        step_id = self._next_step_id()
        target = self.build_locator(role, name, page, step_id)
        step = Step(step_id=step_id, action_type="click", target=target)
        self.steps.append(step)
        return step

    def record_type(
        self, role: str, name: str, text: str, page, param_name: str | None = None
    ) -> Step:
        """`param_name` (from the agent, via the `type` tool) is how a run input gets named -
        the general slot-filler. `{"param_ref": param_name}` flows straight into
        `Capability.input_schema` at compile time. Falls back to the goal-text ID regex only
        when the agent didn't name it."""
        step_id = self._next_step_id()
        target = self.build_locator(role, name, page, step_id)
        value = self._param_value(param_name) if param_name else self._maybe_param_ref(text)
        step = Step(step_id=step_id, action_type="type", target=target, value=value)
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
