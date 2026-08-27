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

from agent.legacy_locate import locate_field_name, locate_labeled_field
from artifact.schema import Capability, ExpectedOutcome, Result, Step
from guardrails.policy import GuardrailViolation, check_risk_confirmation, guardrail_check, redact

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"
_QUOTED_RE = re.compile(r"'([^']*)'")


def _resolve_value(value, params: dict):
    if isinstance(value, dict) and "param_ref" in value:
        param_name = value["param_ref"]
        if param_name not in params:
            raise KeyError(f"missing required param {param_name!r}")
        return params[param_name]
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
            tables = ctx.locator("table")
            table_count = tables.count()
        except Exception:
            continue
        for i in range(table_count):
            table = tables.nth(i)
            try:
                header_row = table.locator("xpath=.//tr[th[@scope='col']]").first
                if header_row.count() == 0:
                    continue
                if header_row.locator("th").all_text_contents() != headers:
                    continue
                data_rows = table.locator("xpath=.//tr[td]")
                if data_rows.count() <= row_index:
                    continue
                cells = data_rows.nth(row_index).locator("td")
                if cells.count() <= column_index:
                    continue
                return cells.nth(column_index)
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


def _check_expected_outcomes(step: Step, page) -> ExpectedOutcome | None:
    """
    Deterministically evaluate each declared condition against the live page's HTML. Every
    condition string this build's compiler emits is of the form "page contains '<substring>'";
    replay checks the literal substring — it never interprets natural language or guesses.
    """
    try:
        content = page.content()
    except Exception:
        content = ""
    for outcome in step.expected_outcomes:
        marker = _extract_quoted_substring(outcome.condition)
        if marker and marker in content:
            return outcome
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


def _execute_step(step: Step, page, params: dict, tier_log: list, outputs: dict, run_id: str) -> Result | None:
    """Returns a Result to short-circuit the run (business outcome / recoverable / hard
    failure), or None to continue to the next step."""

    if step.action_type == "navigate":
        url = _resolve_value(step.value, params)
        try:
            page.goto(url, timeout=step.wait_policy.timeout_ms)
        except Exception as exc:
            outcome = _check_expected_outcomes(step, page)
            if outcome:
                return _outcome_to_result(outcome, outputs)
            return _hard_failure(page, run_id, step.step_id, f"navigate to {url}", str(exc))
        _apply_wait_policy(page, step)
        return None

    if step.action_type in ("click", "type", "select", "extract"):
        locator, tier = _locate(page, step.target)
        tier_log.append({"step_id": step.step_id, "tier": tier or "unresolved"})

        if locator is None:
            outcome = _check_expected_outcomes(step, page)
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
                value = _extract_value(locator)
                outputs[step.extract_as] = value
        except Exception as exc:
            outcome = _check_expected_outcomes(step, page)
            if outcome:
                return _outcome_to_result(outcome, outputs)
            return _hard_failure(page, run_id, step.step_id, "action to succeed", str(exc))

        _apply_wait_policy(page, step)

        # Even a successful action can land on a page matching a declared business/recoverable
        # condition (e.g. a locked-member page renders successfully, it just shows msg-denied
        # instead of the balance) — always check after acting, not only on failure.
        outcome = _check_expected_outcomes(step, page)
        if outcome and outcome.classification != "hard_failure":
            return _outcome_to_result(outcome, outputs)
        return None

    if step.action_type == "wait_for":
        _apply_wait_policy(page, step)
        return None

    if step.action_type == "assert_checkpoint":
        return None  # handled by the capability-level checkpoint check after the loop

    return _hard_failure(page, run_id, step.step_id, "a known action_type", step.action_type)


def _extract_value(locator) -> str:
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


def replay(
    capability: Capability,
    params: dict,
    confirm: bool = False,
    headless: bool = True,
    run_id: str | None = None,
) -> Result:
    run_id = run_id or f"replay_{int(time.time() * 1000)}"

    try:
        check_risk_confirmation(capability.risk_level, confirm)
    except GuardrailViolation as exc:
        return Result(
            status="hard_failure",
            failure_detail={"step_id": None, "expected": "confirm=True for a risky capability", "observed": str(exc)},
        )

    tier_log: list[dict] = []
    outputs: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        try:
            for step in capability.steps:
                action_url = _resolve_value(step.value, params) if step.action_type == "navigate" else None
                try:
                    guardrail_check({"type": step.action_type, "url": action_url},
                                     current_url=page.url, phase="replay")
                except GuardrailViolation as exc:
                    return _hard_failure(page, run_id, step.step_id, "action within allowlist", str(exc))
                except KeyError as exc:
                    return _hard_failure(page, run_id, step.step_id, "all required params provided", str(exc))

                result = _execute_step(step, page, params, tier_log, outputs, run_id)
                if result is not None:
                    return result

            if not _verify_checkpoint(capability, page):
                return _hard_failure(
                    page, run_id, "checkpoint", capability.checkpoint.expected,
                    f"checkpoint not satisfied at final url {page.url}",
                )

            return Result(status="success", outputs=redact(outputs))
        finally:
            print(f"[replay {run_id}] locator tier log: {tier_log}")
            if not headless:
                # headed run -- a human is presumably watching; leave the final page up for a
                # few seconds instead of yanking the window shut the instant the result is ready.
                print(f"[replay {run_id}] leaving the browser open for 5s so you can see the final state...")
                time.sleep(5)
            browser.close()
