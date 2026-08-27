"""
Replay engine (Phase 5) — the deterministic, no-LLM production execution path an AI agent
actually invokes. `replay(capability, params)` walks the capability's recorded Steps in order,
resolves each target with the same 3-tier fallback the recorder logged (role_name -> structural
-> text), and never calls an LLM or guesses anything: every branch it can take (business
outcome / recoverable / hard failure) is decided by literally checking a condition string the
artifact itself declared at compile time (see agent/compiler.py's `_KNOWN_OUTCOMES`).

`replay()` owns its own Playwright browser lifecycle rather than requiring a caller to hand it a
live page — that's what makes it plausible as something an AI agent calls directly as a tool in
production (see mcp_server/ for that surface), not something that needs a pre-existing browser
session threaded through first. `headless=False` exists only for the Phase 7 demo where a
replay hits a hard failure and a human needs to actually take over the same visible window.

Result.status taxonomy (from artifact/schema.py, enforced here, not guessed):
  - "success"            — the checkpoint verified; declared outputs are populated.
  - "business_outcome"   — a step's declared expected_outcomes matched a `business_outcome`
                            condition (e.g. "no such member"). NOT an error — a real, useful
                            answer the caller needs.
  - "recoverable_handled" — a step's declared expected_outcomes matched a `recoverable`
                            condition: a known, transient operational state (a session-timeout
                            page, a rate-limit notice) that isn't a business answer and isn't a
                            system break either. Like `business_outcome`, replay stops cleanly
                            here rather than guessing at a fix in-place -- silently retrying an
                            unrecognized page state is exactly the wrong instinct for a banking
                            replay engine. What it buys the *caller*: a status distinct from
                            `hard_failure` that says "safe to retry the whole run later," not
                            "something is broken, go investigate." None of this build's 5 real
                            capabilities declare one, since this mock app's business logic is
                            fully deterministic and has no naturally-occurring transient state to
                            model -- exercised via `tests/test_replay.py` instead of live replay.
  - "hard_failure"        — nothing declared explains what replay is seeing; stops immediately
                            with step id, expected vs. observed, and a screenshot reference.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from agent.legacy_locate import locate_field_name, locate_labeled_field, locate_labeled_value
from artifact.schema import Capability, ExpectedOutcome, Result, Step
from escalation.controller import trigger_escalation
from guardrails.policy import GuardrailViolation, check_risk_confirmation, guardrail_check, redact
from surface.outcomes import classify as classify_outcome, profile_for

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"
_QUOTED_RE = re.compile(r"'([^']*)'")


def _resolve_value(value, params: dict):
    if isinstance(value, dict) and "param_ref" in value:
        param_name = value["param_ref"]
        if param_name not in params:
            raise KeyError(f"missing required param {param_name!r}")
        return params[param_name]
    # A navigate step whose value is a template string ("…/members/{member_id}/transfer") is
    # filled from params — lets one recorded capability's entry point parameterise the member
    # without baking a concrete id into the URL.
    if isinstance(value, str) and "{" in value and "}" in value:
        try:
            return value.format(**params)
        except (KeyError, IndexError) as exc:
            raise KeyError(f"missing param for URL template {value!r}: {exc}")
    return value


def _contexts(page):
    yield page
    for frame in page.frames:
        if frame != page.main_frame:
            yield frame


def _locate_table_position(page, primary: dict):
    """
    Resolve a table_position locator: find the table whose column headers match, then the
    row_index-th data row, then the column_index-th cell in it. Position-based rather than
    content-based, specifically because a data-table cell with no per-row label has nothing
    stable to anchor on except its own value — which is exactly what changes between replays.
    Returns a Locator or None.
    """
    headers = primary.get("table_headers") or []
    row_index = primary.get("row_index")
    column_index = primary.get("column_index")
    if not headers or row_index is None or column_index is None:
        return None

    for ctx in _contexts(page):
        try:
            handle = ctx.evaluate_handle(
                """([wantHeaders, rowIndex, colIndex]) => {
                    const tables = Array.from(document.querySelectorAll('table'));
                    for (const table of tables) {
                        const rows = Array.from(table.querySelectorAll('tr'))
                            .filter(r => r.closest('table') === table);
                        const headerRow =
                            rows.find(r => r.querySelector('th[scope="col"]')) || rows[0];
                        if (!headerRow) continue;
                        const headers = Array.from(headerRow.querySelectorAll('th, td'))
                            .map(c => c.textContent.trim());
                        if (headers.length !== wantHeaders.length) continue;
                        if (!headers.every((h, i) => h === wantHeaders[i])) continue;
                        const dataRows = rows.filter(
                            r => r !== headerRow && r.querySelector('td'));
                        const tr = dataRows[rowIndex];
                        if (!tr) continue;
                        const cell = tr.children[colIndex];
                        if (!cell) continue;
                        return cell;
                    }
                    return null;
                }""",
                [headers, row_index, column_index],
            )
            element = handle.as_element()
            if element is not None:
                return element
        except Exception:
            continue
    return None


def _locate(page, target):
    """
    Resolve a LocatorTarget the same way the recorder declared it should be found, trying
    role_name/structural (role+name, taking the declared `nth` for structural targets) first,
    then the declared fallbacks, then a bare text-content match as the last resort. Returns
    (locator_or_None, tier_actually_used).
    """
    if target.strategy == "table_position":
        loc = _locate_table_position(page, target.primary)
        return (loc, "table_position") if loc is not None else (None, None)

    if target.strategy == "labeled_field":
        label = target.primary.get("label")
        control_role = target.primary.get("control_role")
        for ctx in _contexts(page):
            loc = locate_labeled_field(ctx, label, control_role)
            if loc is not None:
                try:
                    if loc.count() >= 1:
                        return loc.first, "labeled_field"
                except Exception:
                    continue
        # fall through to the declared fallbacks (field_name, then text)

    if target.strategy == "field_name":
        for ctx in _contexts(page):
            loc = locate_field_name(ctx, target.primary.get("name"))
            if loc is not None:
                return loc, "field_name"

    if target.strategy == "labeled_value":
        for ctx in _contexts(page):
            loc = locate_labeled_value(ctx, target.primary.get("label"))
            if loc is not None:
                return loc, "labeled_value"

    role = target.primary.get("role")
    name = target.primary.get("name")
    nth = target.primary.get("nth", 0)

    if role:
        for ctx in _contexts(page):
            try:
                candidate = ctx.get_by_role(role, name=name)
                count = candidate.count()
            except Exception:
                continue
            if count > nth:
                return candidate.nth(nth), target.strategy

    for fallback in target.fallbacks:
        if fallback.get("strategy") == "field_name" and fallback.get("name"):
            for ctx in _contexts(page):
                loc = locate_field_name(ctx, fallback["name"])
                if loc is not None:
                    return loc, "field_name (fallback)"
            continue
        ftext = fallback.get("text")
        if not ftext:
            continue
        for ctx in _contexts(page):
            try:
                candidate = ctx.get_by_text(ftext, exact=False)
                if candidate.count() > 0:
                    return candidate.first, "text (fallback)"
            except Exception:
                continue

    text = target.primary.get("text")
    if text:
        for ctx in _contexts(page):
            try:
                candidate = ctx.get_by_text(text, exact=False)
                if candidate.count() > 0:
                    return candidate.first, "text"
            except Exception:
                continue

    return None, None


def _extract_quoted_substring(condition: str) -> str | None:
    match = _QUOTED_RE.search(condition)
    return match.group(1) if match else None


def _check_expected_outcomes(step: Step, page, run_ctx: dict | None = None) -> ExpectedOutcome | None:
    """
    Deterministically decide whether the live page represents a declared/known outcome, in this
    order:
      1. the step's own `expected_outcomes` — literal "page contains '<substring>'" checks;
      2. the target's runtime taxonomy (surface/outcomes.py) — the main-frame document HTTP
         status (deterministic, copy-independent), then target-wide body-text conditions.
    Never interprets natural language, never calls an LLM.
    """
    try:
        content = page.content()
    except Exception:
        content = ""
    for outcome in step.expected_outcomes:
        marker = _extract_quoted_substring(outcome.condition)
        if marker and marker in content:
            return outcome
    if run_ctx is not None:
        return classify_outcome(run_ctx.get("profile"), run_ctx.get("http_status"), content)
    return None


def _save_failure_screenshot(page, run_id: str, step_id: str) -> str | None:
    EVIDENCE_DIR.mkdir(exist_ok=True)
    path = EVIDENCE_DIR / f"replay_{run_id}_failure_{step_id}.png"
    try:
        page.screenshot(path=str(path))
        return f"evidence/{path.name}"
    except Exception:
        return None


def _hard_failure(page, run_id: str, step_id: str, expected: str, observed: str) -> Result:
    screenshot_ref = _save_failure_screenshot(page, run_id, step_id)
    return Result(
        status="hard_failure",
        failure_detail=redact({
            "step_id": step_id, "expected": expected, "observed": observed,
            "screenshot_ref": screenshot_ref,
        }),
        evidence_ref=screenshot_ref,
    )


def _apply_wait_policy(page, step: Step):
    """
    Retries are only applied for steps explicitly tagged `retry_on: transient_load` — replay
    does not blindly retry everything, only what the artifact declares as expected to sometimes
    need it.
    """
    if "transient_load" not in step.wait_policy.retry_on:
        return
    for _ in range(step.wait_policy.retry_count):
        try:
            page.wait_for_load_state("networkidle", timeout=step.wait_policy.timeout_ms)
            return
        except Exception:
            continue


def _execute_step(step: Step, page, params: dict, tier_log: list, outputs: dict, run_id: str,
                  run_ctx: dict) -> Result | None:
    """Returns a Result to short-circuit the run (business outcome / recoverable / hard
    failure), or None to continue to the next step."""

    if step.action_type == "navigate":
        url = _resolve_value(step.value, params)
        run_ctx["http_status"] = None
        try:
            resp = page.goto(url, timeout=step.wait_policy.timeout_ms)
            run_ctx["http_status"] = resp.status if resp is not None else None
        except Exception as exc:
            outcome = _check_expected_outcomes(step, page, run_ctx)
            if outcome:
                return _outcome_to_result(outcome, outputs)
            return _hard_failure(page, run_id, step.step_id, f"navigate to {url}", str(exc))
        _apply_wait_policy(page, step)
        outcome = _check_expected_outcomes(step, page, run_ctx)
        if outcome:
            return _outcome_to_result(outcome, outputs)
        return None

    if step.action_type in ("click", "type", "select", "extract"):
        # a click can trigger navigation — reset so the response listener's value is this step's
        run_ctx["http_status"] = None
        locator, tier = _locate(page, step.target)
        tier_log.append({"step_id": step.step_id, "tier": tier or "unresolved"})

        if locator is None:
            outcome = _check_expected_outcomes(step, page, run_ctx)
            if outcome:
                return _outcome_to_result(outcome, outputs)
            return _hard_failure(
                page, run_id, step.step_id,
                f"element for {step.target.primary}", "no element resolved on the live page",
            )

        try:
            if step.action_type == "click":
                locator.click(timeout=step.wait_policy.timeout_ms)
            elif step.action_type in ("type", "select"):
                value = _resolve_value(step.value, params)
                try:
                    locator.fill(value, timeout=step.wait_policy.timeout_ms)
                except Exception:
                    locator.select_option(value, timeout=step.wait_policy.timeout_ms)
            elif step.action_type == "extract":
                value = _extract_value(locator, step.target.strategy if step.target else None)
                outputs[step.extract_as] = value
        except Exception as exc:
            outcome = _check_expected_outcomes(step, page, run_ctx)
            if outcome:
                return _outcome_to_result(outcome, outputs)
            return _hard_failure(page, run_id, step.step_id, "action to succeed", str(exc))

        if step.action_type == "click":
            # a server-rendered form submit navigates — let it settle so the HTTP status and
            # page content the outcome check reads are the post-navigation ones
            try:
                page.wait_for_load_state("load", timeout=step.wait_policy.timeout_ms)
            except Exception:
                pass
        _apply_wait_policy(page, step)

        # Even a successful action can land on a known outcome page — a validation-error render
        # after a Continue click (HTTP 400), a "Source share is HOLD" body, a 500. Check after
        # acting, not only on failure, and short-circuit on any classification including
        # hard_failure (a 500 after a technically-successful navigation is still a hard failure).
        outcome = _check_expected_outcomes(step, page, run_ctx)
        if outcome:
            return _outcome_to_result(outcome, outputs)
        return None

    if step.action_type == "wait_for":
        _apply_wait_policy(page, step)
        return None

    if step.action_type == "assert_checkpoint":
        return None  # handled by the capability-level checkpoint check after the loop

    return _hard_failure(page, run_id, step.step_id, "a known action_type", step.action_type)


def _extract_value(locator, strategy: str | None = None) -> str:
    """
    Read the value at a resolved locator. The right rule depends on how the locator was
    declared:

    - table_position / labeled_field / field_name: the locator already points at the exact
      cell or the exact form control — return its own value/text. The row-relative heuristic
      below would be wrong (it returns the row's *first* cell regardless).
    - role_name / structural / text: the recorder anchored on a label (a `<th scope="row">`),
      so the value is in a sibling `<td>` in the same row.
    """
    if strategy in ("table_position", "labeled_field", "field_name", "labeled_value"):
        try:
            value = locator.input_value(timeout=1000)
            if value:
                return value
        except Exception:
            pass
        return (locator.text_content() or "").strip()

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
    return (locator.text_content() or "").strip()


def _outcome_to_result(outcome: ExpectedOutcome, outputs: dict) -> Result:
    if outcome.classification == "business_outcome":
        return Result(status="business_outcome", business_outcome_code=outcome.code, outputs=redact(outputs))
    if outcome.classification == "recoverable":
        return Result(status="recoverable_handled", outputs=redact(outputs))
    # classification == "hard_failure": declared-but-fatal condition actually observed
    return Result(
        status="hard_failure",
        business_outcome_code=outcome.code,
        failure_detail={"expected": "a recoverable/handled state", "observed": outcome.condition},
    )


def _verify_checkpoint(capability: Capability, page) -> bool:
    cp = capability.checkpoint
    try:
        if cp.type == "url_match":
            return cp.expected in page.url
        if cp.type == "text_match":
            return cp.expected in page.content()
        if cp.type == "element_present":
            role = (cp.locator or {}).get("role")
            name = (cp.locator or {}).get("name")
            for ctx in _contexts(page):
                try:
                    if ctx.get_by_role(role, name=name).count() > 0:
                        return True
                except Exception:
                    continue
            return False
    except Exception:
        return False
    return False


def _last_click_step_id(capability: Capability) -> str | None:
    """The irreversible commit on a review->post flow is the final click (Post Transfer / Post
    Hold / Confirm). Used as the escalation point when a risky capability is invoked in
    `escalate` mode instead of with `confirm=True`."""
    for step in reversed(capability.steps):
        if step.action_type == "click":
            return step.step_id
    return None


def _walk_capability(capability: Capability, params: dict, page, run_id: str,
                     tier_log: list, outputs: dict,
                     escalate_before: str | None = None,
                     escalation_max_wait_s: float | None = None) -> Result:
    """Walk the capability's Steps against a live `page` and verify the checkpoint. No browser
    lifecycle here — the caller owns the page (see `replay`, and `agent/session.py` which runs a
    signon capability then a target capability on one shared authenticated context).

    If `escalate_before` is a step id, replay pauses there and routes an intervention request to
    the operator console (escalation/controller.trigger_escalation) before taking that step —
    this is how a risky capability invoked through the API gets a human in the loop without a
    caller-supplied `confirm=True`.
    """
    run_ctx = {"profile": profile_for(capability.target), "http_status": None}
    # capture the main-frame document HTTP status of click-triggered navigations (page.goto's
    # status is read directly in _execute_step)
    def _on_response(resp):
        try:
            if resp.request.is_navigation_request() and resp.frame is page.main_frame:
                run_ctx["http_status"] = resp.status
        except Exception:
            pass
    try:
        page.on("response", _on_response)
    except Exception:
        pass

    try:
        for step in capability.steps:
            if escalate_before and step.step_id == escalate_before:
                reason = (f"risky capability {capability.capability_id!r} invoked without "
                          f"confirm — pausing before step {step.step_id} "
                          f"({step.action_type} {(step.target.primary if step.target else '')}) "
                          "for human approval of the irreversible action.")
                lease = trigger_escalation(reason, page, run_id=run_id,
                                           max_wait_s=escalation_max_wait_s)
                decision = lease.context.get("decision")
                note = lease.context.get("human_actions_summary", "")
                if decision != "approved":
                    return Result(
                        status="escalated",
                        business_outcome_code=None,
                        failure_detail=redact({
                            "step_id": step.step_id,
                            "expected": "operator approval of the irreversible action",
                            "observed": f"operator decision={decision!r} ({note or 'no note'})",
                        }),
                    )

            action_url = _resolve_value(step.value, params) if step.action_type == "navigate" else None
            try:
                guardrail_check({"type": step.action_type, "url": action_url},
                                 current_url=page.url, phase="replay")
            except GuardrailViolation as exc:
                return _hard_failure(page, run_id, step.step_id, "action within allowlist", str(exc))
            except KeyError as exc:
                return _hard_failure(page, run_id, step.step_id, "all required params provided", str(exc))

            result = _execute_step(step, page, params, tier_log, outputs, run_id, run_ctx)
            if result is not None:
                return result

        if not _verify_checkpoint(capability, page):
            return _hard_failure(
                page, run_id, "checkpoint", capability.checkpoint.expected,
                f"checkpoint not satisfied at final url {page.url}",
            )
        return Result(status="success", outputs=redact(outputs))
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass


def replay(
    capability: Capability,
    params: dict,
    confirm: bool = False,
    headless: bool = True,
    run_id: str | None = None,
    page=None,
    risky_mode: str = "confirm",
    escalation_max_wait_s: float | None = None,
) -> Result:
    """
    Deterministic, no-LLM replay of a capability. Owns its own Playwright browser unless `page`
    is supplied — passing an existing (already-authenticated) page lets a caller compose a
    signon capability and a target capability on one session without this function knowing about
    sessions (see agent/session.py). When `page` is supplied the caller also owns closing it.

    `risky_mode` decides how a `risk_level: risky` capability is gated when `confirm` is False:
      - "confirm"  (default, take-home behaviour): pre-flight refuse with a hard_failure.
      - "escalate" (the API path): run up to the final click, then route an intervention request
        to the operator console and only take that step if a human approves; a decline returns
        status="escalated".
    """
    run_id = run_id or f"replay_{int(time.time() * 1000)}"

    escalate_before = None
    if capability.risk_level == "risky" and not confirm:
        if risky_mode == "escalate":
            escalate_before = _last_click_step_id(capability)
        else:
            try:
                check_risk_confirmation(capability.risk_level, confirm)
            except GuardrailViolation as exc:
                return Result(
                    status="hard_failure",
                    failure_detail={"step_id": None, "expected": "confirm=True for a risky capability",
                                    "observed": str(exc)},
                )

    tier_log: list[dict] = []
    outputs: dict = {}
    _walk = lambda pg: _walk_capability(  # noqa: E731
        capability, params, pg, run_id, tier_log, outputs,
        escalate_before=escalate_before, escalation_max_wait_s=escalation_max_wait_s,
    )

    if page is not None:
        try:
            return _walk(page)
        finally:
            print(f"[replay {run_id}] locator tier log: {tier_log}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        try:
            return _walk(page)
        finally:
            print(f"[replay {run_id}] locator tier log: {tier_log}")
            if not headless:
                # headed run -- a human is presumably watching; leave the final page up for a
                # few seconds instead of yanking the window shut the instant the result is ready.
                print(f"[replay {run_id}] leaving the browser open for 5s so you can see the final state...")
                time.sleep(5)
            browser.close()
