"""
The 6 tools the discovery LLM can call, in Anthropic tool-use schema form, plus the Python
functions that execute each one against a live Playwright page. These are deliberately a
*different* surface from what gets replayed (artifact/schema.py's Step/LocatorTarget) — the
Recorder (agent/recorder.py) translates an accepted tool call into a Step, tool execution here
is just "make the browser do the thing right now."

Locating elements: every tool that acts on the page (click, type, extract) resolves role+name
against the main frame first, then falls back to searching every child frame — this is what
lets `click(role="button", name="Confirm and Open Account")` work whether or not that button
happens to be inside the confirmation iframe, without the LLM needing to know or care about
frame boundaries (it only ever sees the merged accessibility tree from perception.py).
"""
from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError, Page

from agent.legacy_locate import locate_labeled_field

TOOLS = [
    {
        "name": "click",
        "description": (
            "Click an element identified by its accessibility role and accessible name (the "
            "visible label text). Works for elements inside iframes transparently."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "ARIA role, e.g. 'button', 'link'"},
                "name": {"type": "string", "description": "accessible name / visible label"},
            },
            "required": ["role", "name"],
        },
    },
    {
        "name": "type",
        "description": (
            "Type text into a form field (textbox, etc.) identified by its accessibility role "
            "and accessible name — for a labelled field, the name is the label text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["role", "name", "text"],
        },
    },
    {
        "name": "navigate",
        "description": (
            "Navigate the browser directly to a URL. Prefer clicking through the UI when "
            "possible; use this mainly for the initial entry point."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "extract",
        "description": (
            "Read a data value off the current page, identified by the role+name of its label "
            "(e.g. a table row-header like 'Savings Balance'), and store it under a variable "
            "name to include in the final output."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "name": {"type": "string"},
                "as_var": {"type": "string", "description": "variable name to store the value under"},
            },
            "required": ["role", "name", "as_var"],
        },
    },
    {
        "name": "finish",
        "description": (
            "Call this when the goal has been fully accomplished, OR a definitive business "
            "outcome has been reached (e.g. 'no such member' — that is a real, useful answer, "
            "not a failure). success=false should be used only when the goal is genuinely "
            "impossible with the actions available, not for a business outcome."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "outputs": {
                    "type": "object",
                    "description": "the extracted variables to return, e.g. {\"savings_balance\": \"$1,842.30\"}",
                },
                "business_outcome_code": {
                    "type": ["string", "null"],
                    "description": "e.g. MEMBER_NOT_FOUND, PERMISSION_DENIED — set when this run's result is a business outcome rather than a plain success",
                },
                "summary": {"type": "string"},
            },
            "required": ["success", "summary"],
        },
    },
    {
        "name": "escalate",
        "description": (
            "Call this when you cannot safely proceed on your own: you are stuck (repeated "
            "actions aren't changing the page), the UI shows something unexpected you don't "
            "understand, or the next step is risky/irreversible and requires a human's "
            "explicit confirmation. Never take a risky/irreversible action directly — escalate "
            "instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]


class ToolExecutionError(Exception):
    """Raised when a tool call can't be carried out (element not found, navigation failed, ...)."""


def _contexts(page: Page):
    """Main frame first, then every child frame — the order defines locate priority."""
    yield page
    for frame in page.frames:
        if frame != page.main_frame:
            yield frame


def locate_by_role_name(page: Page, role: str, name: str):
    """
    Resolve (role, name) to a single Locator, trying the main frame then each child frame in
    order. Returns (locator, context_label) or raises ToolExecutionError if nothing matches
    anywhere. context_label is "main" or the frame's URL, useful for logging/evidence.
    """
    role_norm = role.lower()
    for ctx in _contexts(page):
        try:
            candidate = ctx.get_by_role(role_norm, name=name)
            count = candidate.count()
        except Exception:
            continue
        if count >= 1:
            label = "main" if ctx is page else ctx.url
            return candidate.first, label

    # Legacy fallback: a form control with no accessible name, addressed by the visible label
    # text next to it (agent/legacy_locate.py). This is what carries MERIDIAN CORE, whose inputs
    # have no <label for>/aria-label/placeholder — perception.py surfaces such a control to the
    # model under its derived label, and this resolves that label back to the element.
    for ctx in _contexts(page):
        try:
            loc = locate_labeled_field(ctx, name, control_role=role_norm)
        except Exception:
            loc = None
        if loc is not None:
            where = "main (labeled_field)" if ctx is page else f"{ctx.url} (labeled_field)"
            return loc.first, where

    raise ToolExecutionError(f"no element found for role={role!r} name={name!r} on page or in any frame")


def execute_click(page: Page, role: str, name: str) -> str:
    if role.lower() == "option":
        raise ToolExecutionError(
            f"cannot click a dropdown option ({name!r}). To choose it, use the `type` tool on "
            "the combobox: type(role='combobox', name='<the dropdown label>', text='<option "
            "text or value>')."
        )
    locator, where = locate_by_role_name(page, role, name)
    try:
        locator.click(timeout=5000)
    except PlaywrightError as exc:
        # Catches PlaywrightTimeoutError too (it subclasses Error) plus every other real
        # Playwright failure (element detached, not visible, not clickable, ...) -- narrowing
        # this to TimeoutError only meant any of those instead propagated straight out of
        # run_discovery() uncaught, crashing the whole run instead of a graceful tool_error the
        # model could see and reason about. Found by inspection, not a live crash.
        raise ToolExecutionError(f"click on role={role!r} name={name!r} failed: {exc}") from exc
    return f"clicked {role} '{name}' (in {where})"


def execute_type(page: Page, role: str, name: str, text: str) -> str:
    """
    Two real element shapes share the "type" tool: a plain text input (`fill`) and a `<select>`
    (resolves to role "combobox", needs `select_option`, not `fill` -- `fill()` raises
    immediately with a plain `Error`, not a timeout, verified directly against a real Playwright
    page, which is exactly why the fallback below has to catch `PlaywrightError`, not just
    a timeout: the original narrower except never actually caught it, so this fallback was dead
    code -- the model could never successfully select a non-default option, it would just crash
    the run instead. `select_option` itself is tried by value first (the HTML attribute, e.g.
    "christmas_club") then by visible label (e.g. "Christmas Club"), since the model only ever
    sees the visible label text via the accessibility tree, not the underlying value attribute.
    """
    locator, where = locate_by_role_name(page, role, name)
    try:
        locator.fill(text, timeout=5000)
    except PlaywrightError:
        try:
            locator.select_option(value=text, timeout=5000)
        except PlaywrightError:
            try:
                locator.select_option(label=text, timeout=5000)
            except PlaywrightError as exc:
                raise ToolExecutionError(
                    f"type into role={role!r} name={name!r} failed (tried fill, select by "
                    f"value, and select by label): {exc}"
                ) from exc
    return f"typed {text!r} into {role} '{name}' (in {where})"


def execute_navigate(page: Page, url: str) -> str:
    try:
        page.goto(url, timeout=10000)
    except PlaywrightError as exc:
        raise ToolExecutionError(f"navigate to {url!r} failed: {exc}") from exc
    return f"navigated to {url}"


def execute_extract(page: Page, role: str, name: str) -> str:
    """
    Reads a value off the page.

    - role "cell"/"gridcell": the model is pointing at a specific data-table cell (MERIDIAN
      CORE's SHARES/BALANCES table, transaction lists) — return that cell's own text. The
      row-relative heuristic below is wrong here: it would always return the row's *first*
      cell regardless of which one was asked for.
    - role "rowheader"/"th" label (the take-home's `<th scope="row">Savings Balance</th>`
      shape): the value lives in a sibling `<td>` in the same row — walk to the row and take
      the first cell that isn't the label.
    - anything else: the anchor's own form value, then its own text content.
    """
    locator, _ = locate_by_role_name(page, role, name)

    if role.lower() in ("cell", "gridcell"):
        return (locator.text_content() or "").strip()

    if role.lower() in ("rowheader", "columnheader"):
        try:
            row_value_cell = locator.locator("xpath=ancestor::tr[1]//td[1]")
            if row_value_cell.count() > 0:
                text = row_value_cell.first.text_content()
                if text and text.strip():
                    return text.strip()
        except Exception:
            pass

    try:
        value = locator.input_value(timeout=1000)
        if value:
            return value
    except Exception:
        pass

    text = locator.text_content() or ""
    return text.strip()
