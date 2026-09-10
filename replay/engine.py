"""
Replay engine (Phase 5) - the deterministic, no-LLM production execution path an AI agent
actually invokes. `replay(capability, params)` walks the capability's recorded Steps in order,
resolves each target with the same 3-tier fallback the recorder logged (role_name -> structural
-> text), and never calls an LLM or guesses anything: every branch it can take (business
outcome / recoverable / hard failure) is decided by literally checking a condition string the
artifact itself declared at compile time (from app_knowledge/<app>.yaml + agent proposals; see
agent/compiler.py).

`replay()` owns its own Playwright browser lifecycle rather than requiring a caller to hand it a
live page - that's what makes it plausible as something an AI agent calls directly as a tool in
production (see mcp_server/ for that surface), not something that needs a pre-existing browser
session threaded through first. `headless=False` exists only for the Phase 7 demo where a
replay hits a hard failure and a human needs to actually take over the same visible window.

Result.status taxonomy (from artifact/schema.py, enforced here, not guessed):
  - "success" - the checkpoint verified; declared outputs are populated.
  - "business_outcome" - a step's declared expected_outcomes matched a `business_outcome`
                            condition (e.g. "no such member"). NOT an error - a real, useful
                            answer the caller needs.
  - "recoverable_handled" - a step's declared expected_outcomes matched a `recoverable`
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
  - "data_unavailable" - replay reached the page and it was structurally intact, but the datum
                            a step exists to read is not actually present: an empty or
                            placeholder cell whose declared `extract_contract` it fails, a
                            `ready_when` data region that never populated within the wait budget,
                            or a declared `data_unavailable` page condition (a degraded-service
                            banner). Distinct from `hard_failure` - nothing is broken, so
                            investigating the automation won't help - and from `success` with a
                            junk value. What it buys the caller: a status that says "the page is
                            fine, the data isn't there right now" - retry later, read it from
                            another source, or tell its own user it's unavailable. This is the
                            "page access, no data access" case generalized: a first-class,
                            declared, deterministic outcome instead of a per-capability patch
                            each time an empty render slips through as success.
  - "hard_failure" - nothing declared explains what replay is seeing; stops immediately
                            with step id, expected vs. observed, and a screenshot reference.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from artifact.schema import Capability, ExpectedOutcome, ExtractContract, Result, Step
from common.browser import LAUNCH_ARGS
from common.logging import get_logger
from escalation import policy as esc_policy
from escalation.controller import trigger_escalation
from guardrails.policy import GuardrailViolation, check_risk_confirmation, guardrail_check, redact
from replay import idempotency

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"
_QUOTED_RE = re.compile(r"'([^']*)'")
_READY_POLL_INTERVAL_S = 0.25
_log = get_logger("replay")


def _resolve_value(value, params: dict):
    if isinstance(value, dict) and "param_ref" in value:
        param_name = value["param_ref"]
        if param_name not in params:
            raise KeyError(f"missing required param {param_name!r}")
        resolved = params[param_name]
        template = value.get("url_template")
        if template:
            # a partly-parameterized navigation, e.g. "http://host/member/{member_id}/txns"
            return template.format(**{param_name: resolved})
        return resolved
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
    stable to anchor on except its own value - which is exactly what changes between replays.
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


def _await_ready(step: Step, page) -> bool:
    """
    If the step declares a `ready_when` marker ("page contains '<substring>'"), poll the live
    page until it appears or the step's timeout budget runs out. No marker declared -> ready
    immediately. Returns False only when a marker was declared and never showed up - replay's
    signal that the page shell loaded but the data region behind it never populated, which is a
    `data_unavailable`, not a broken locator.
    """
    if not step.ready_when:
        return True
    marker = _extract_quoted_substring(step.ready_when)
    if not marker:
        return True
    deadline = time.monotonic() + step.wait_policy.timeout_ms / 1000
    while True:
        try:
            if marker in page.content():
                return True
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(_READY_POLL_INTERVAL_S)


def _validate_extracted(value, contract: ExtractContract) -> str | None:
    """
    Check an extracted value against its declared ExtractContract. Returns a human-readable
    reason string when the value is NOT real data (empty, a declared placeholder, or the wrong
    shape), or None when it passes. A malformed `pattern` in the artifact is treated as
    unchecked rather than crashing the run.
    """
    text = (value or "").strip() if isinstance(value, str) else ("" if value is None else str(value).strip())
    if contract.nonempty and not text:
        return "extracted value is empty - the element rendered but carried no data behind it"
    if text and text in contract.placeholders:
        return f"extracted value {text!r} is a placeholder ({contract.placeholders}), not real data"
    if contract.pattern:
        try:
            if not re.fullmatch(contract.pattern, text):
                return (
                    f"extracted value {text!r} does not match the declared data shape "
                    f"{contract.pattern!r} - likely a placeholder or an unrendered field"
                )
        except re.error:
            return None
    return None


def _check_expected_outcomes(step: Step, page) -> ExpectedOutcome | None:
    """
    Deterministically evaluate each declared condition against the live page's HTML. Every
    condition string this build's compiler emits is of the form "page contains '<substring>'";
    replay checks the literal substring - it never interprets natural language or guesses.
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


def _save_failure_screenshot(page, run_id: str, step_id: str, kind: str = "failure") -> str | None:
    EVIDENCE_DIR.mkdir(exist_ok=True)
    path = EVIDENCE_DIR / f"replay_{run_id}_{kind}_{step_id}.png"
    try:
        page.screenshot(path=str(path))
        return f"evidence/{path.name}"
    except Exception:
        return None


def _write_trace(run_id: str, capability: Capability, params: dict, trace: list[dict], result: Result) -> None:
    """
    Append-free JSONL run-log: one header line, then one line per executed step. This is the
    substrate for drift detection (the `tier` column) and latency tracking (the `ms` column)
    without any extra infrastructure - `replay/metrics.py` aggregates a directory of these into
    per-capability rates and p50/p95. Never raises: a failed evidence write must not fail a run.
    """
    try:
        EVIDENCE_DIR.mkdir(exist_ok=True)
        path = EVIDENCE_DIR / f"replay_{run_id}_trace.jsonl"
        header = {
            "kind": "replay_trace_header", "run_id": run_id,
            "capability_id": capability.capability_id, "version": capability.version,
            "param_keys": sorted(params.keys()), "status": result.status,
            "business_outcome_code": result.business_outcome_code,
            "total_ms": round(sum(row.get("ms", 0.0) for row in trace), 1),
            "ts": time.time(),
        }
        lines = [json.dumps(header)] + [json.dumps({"kind": "step", **row}) for row in trace]
        path.write_text("\n".join(lines) + "\n")
    except Exception as exc:
        _log.warning("trace_write_failed", run_id=run_id, error=str(exc))


def _data_unavailable(page, run_id: str, step_id: str, detail: dict) -> Result:
    """
    The page was reachable and structurally intact, but the datum this step exists to read
    isn't actually present. NOT `hard_failure` (nothing is broken - investigating won't help)
    and NOT `success` with a junk value. A distinct status the caller can act on: retry later,
    fall back to another source, or surface "not available right now" to its own user.
    """
    screenshot_ref = _save_failure_screenshot(page, run_id, step_id, kind="dataunavailable")
    return Result(
        status="data_unavailable",
        failure_detail=redact({"step_id": step_id, **detail, "screenshot_ref": screenshot_ref}),
        evidence_ref=screenshot_ref,
    )


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
    Retries are only applied for steps explicitly tagged `retry_on: transient_load` - replay
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


def _slug(name: str) -> str:
    """A field's accessible name -> a param-ish key, e.g. 'Opening Deposit ($)' -> 'opening_deposit'.
    Used so an override can be matched by escalation-policy `param` rules."""
    out = "".join(c.lower() if c.isalnum() else " " for c in (name or ""))
    return "_".join(out.split())


def _execute_step(step: Step, page, params: dict, tier_log: list, outputs: dict, run_id: str,
                  overrides: dict | None = None) -> Result | None:
    """Returns a Result to short-circuit the run (business outcome / recoverable / hard
    failure), or None to continue to the next step.

    `overrides` maps a `step_id` to a literal value that replaces whatever a `type`/`select`
    step recorded - this is how a replay runs with a different deposit amount, dispute reason,
    address, etc. without re-discovering the capability."""
    overrides = overrides or {}

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
        if not _await_ready(step, page):
            return _data_unavailable(page, run_id, step.step_id, {
                "expected": f"readiness marker {step.ready_when!r} after navigating to {url}",
                "observed": "marker never appeared within the wait budget",
                "reason": "the page responded but the expected content never rendered",
            })
        return None

    if step.action_type in ("click", "type", "select", "extract"):
        if not _await_ready(step, page):
            outcome = _check_expected_outcomes(step, page)
            if outcome and outcome.classification != "hard_failure":
                return _outcome_to_result(outcome, outputs)
            return _data_unavailable(page, run_id, step.step_id, {
                "expected": f"readiness marker {step.ready_when!r} before this step",
                "observed": "marker never appeared within the wait budget",
                "reason": "the page shell loaded but the data region this step reads never populated",
            })

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
                value = overrides[step.step_id] if step.step_id in overrides \
                    else _resolve_value(step.value, params)
                try:
                    locator.fill(value, timeout=step.wait_policy.timeout_ms)
                except Exception:
                    locator.select_option(value, timeout=step.wait_policy.timeout_ms)
            elif step.action_type == "extract":
                value = _extract_value(locator)
                # _extract_value's last-resort fallback returns the locator's own text when the
                # value cell is empty - for a label-anchored extract that surfaces the label
                # ("Savings Balance") as if it were the value. Read that as "no data" so the
                # contract check reports an empty cell, not a mislabelled one.
                label = (step.target.primary.get("name") or "").strip() if step.target else ""
                if label and value == label:
                    value = ""
                outputs[step.extract_as] = value
        except Exception as exc:
            outcome = _check_expected_outcomes(step, page)
            if outcome:
                return _outcome_to_result(outcome, outputs)
            return _hard_failure(page, run_id, step.step_id, "action to succeed", str(exc))

        _apply_wait_policy(page, step)

        # Even a successful action can land on a page matching a declared business/recoverable
        # condition (e.g. a locked-member page renders successfully, it just shows msg-denied
        # instead of the balance) - always check after acting, not only on failure. A declared
        # condition is the most specific, human-authored signal, so it wins first.
        outcome = _check_expected_outcomes(step, page)
        if outcome and outcome.classification != "hard_failure":
            return _outcome_to_result(outcome, outputs)

        # Then, for an extract: confirm the value we actually pulled is real data, not a blank
        # or placeholder cell. The locator resolved and the action "succeeded" - the page was
        # there, the datum behind it was not. That's `data_unavailable`, not a silent success.
        if step.action_type == "extract" and step.extract_contract:
            reason = _validate_extracted(outputs.get(step.extract_as), step.extract_contract)
            if reason:
                return _data_unavailable(page, run_id, step.step_id, {
                    "extract_as": step.extract_as,
                    "observed": outputs.get(step.extract_as),
                    "reason": reason,
                })
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
    if outcome.classification == "data_unavailable":
        # a known page string that means "the data isn't here right now" (a degraded-service
        # banner, an empty-state panel) - declared the same way as any other outcome, routed to
        # the distinct status the caller branches on.
        return Result(
            status="data_unavailable",
            business_outcome_code=outcome.code,
            failure_detail={"expected": "real data on the page", "observed": outcome.condition},
            outputs=redact(outputs),
        )
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


def _precheck(
    capability: Capability, confirm: bool, idempotency_key: str | None,
    allow_escalation: bool = False,
) -> tuple[Result | None, bool]:
    """Cheap gates that run before any browser work: the risk-confirmation check and the
    idempotency-ledger lookup. Returns `(short_circuit_result_or_None, ledgered)` - a non-None
    Result means "don't launch a browser, return this."

    When `allow_escalation` is set (a live operator is available), a risky capability without
    `confirm=True` is NOT refused here - the per-step escalation policy pauses it for a human
    instead. Without an operator (CLI, CI, tests) the old refusal stands."""
    if not allow_escalation:
        try:
            check_risk_confirmation(capability.risk_level, confirm)
        except GuardrailViolation as exc:
            return Result(
                status="hard_failure",
                failure_detail={"step_id": None, "expected": "confirm=True for a risky capability",
                                "observed": str(exc)},
            ), False

    ledgered = capability.risk_level == "risky" and bool(idempotency_key)
    if ledgered:
        prior = idempotency.lookup(idempotency_key)
        if prior is not None:
            return Result.model_validate(prior["result"]).model_copy(
                update={"idempotent_replay": True}
            ), True
    return None, ledgered


def _run_on_page(
    capability: Capability, params: dict, page, *,
    run_id: str, ledgered: bool, idempotency_key: str | None, on_event=None,
    confirm: bool = False, allow_escalation: bool = False, overrides: dict | None = None,
) -> Result:
    """Walk the capability's steps against an already-open `page` and return the Result. Owns
    the trace + checkpoint + idempotency-record; does NOT own the browser lifecycle, so it can
    run either on a per-call browser (`replay`) or a warm pooled context (`BrowserPool.run`).

    `on_event(dict)` - if given, called as each step completes and once with the final result;
    used by webconsole/ to stream a replay live. Never raises out of the loop.

    Before every state-changing step the escalation policy (escalation/policy.py) is consulted.
    `block` -> hard_failure; `escalate` -> pause for a human via the lease when
    `allow_escalation` and not `confirm` (a declined step ends the run as
    business_outcome/OPERATOR_DECLINED)."""
    _policy, _escalate = esc_policy, trigger_escalation
    overrides = overrides or {}

    # overrides are keyed by step_id; also expose them to policy `param` rules under a slug of
    # the field name (e.g. step s10 "Opening Deposit ($)" -> params key "opening_deposit").
    _by_id = {s.step_id: s for s in capability.steps}
    policy_params = dict(params)
    for sid, val in overrides.items():
        st = _by_id.get(sid)
        if st is not None and st.target:
            policy_params[_slug(st.target.primary.get("name"))] = val

    tier_log: list[dict] = []
    trace: list[dict] = []
    outputs: dict = {}

    def _emit(ev: dict) -> None:
        if on_event is not None:
            try:
                on_event(ev)
            except Exception:
                pass

    def _policy_gate(step) -> Result | None:
        """Returns a terminal Result to stop the run, or None to proceed with this step."""
        if step.action_type not in ("click", "type", "select"):
            return None
        ctx = {
            "capability_id": capability.capability_id,
            "risk_level": capability.risk_level,
            "target_app": capability.target.app_name,
            "step": {"action_type": step.action_type,
                     "name": step.target.primary.get("name") if step.target else None},
            "params": policy_params,
        }
        d = _policy.evaluate(ctx)
        _emit({"type": "policy_check", "step_id": step.step_id, "decision": d.action,
               "rule": d.rule_id, "reason": d.reason})
        if d.action == "allow":
            return None
        if d.action == "block":
            return _hard_failure(page, run_id, step.step_id,
                                 "a step the escalation policy permits", d.reason)
        # escalate
        if confirm or not allow_escalation:
            _emit({"type": "policy_check", "step_id": step.step_id, "decision": "allow",
                   "rule": d.rule_id,
                   "reason": ("proceeding: confirm=True given" if confirm
                              else "proceeding: no live operator (allow_escalation=False)")})
            return None
        _emit({"type": "escalate_requested", "step_id": step.step_id, "reason": d.reason})
        lease = _escalate(d.reason, page, run_id=run_id)
        decision = (lease.context or {}).get("decision")
        _emit({"type": "escalation_resumed", "step_id": step.step_id, "decision": decision})
        if decision == "declined":
            return Result(status="business_outcome", business_outcome_code="OPERATOR_DECLINED",
                          outputs=redact(outputs))
        return None

    def _tier_for(step_id: str) -> str | None:
        return next((e["tier"] for e in reversed(tier_log) if e["step_id"] == step_id), None)

    _emit({"type": "replay_start", "run_id": run_id, "capability_id": capability.capability_id,
           "params": params, "overrides": overrides, "steps": len(capability.steps)})
    final: Result | None = None
    for step in capability.steps:
        if step.step_id in overrides:
            _emit({"type": "override_applied", "step_id": step.step_id,
                   "field": step.target.primary.get("name") if step.target else None,
                   "value": overrides[step.step_id]})
        gate = _policy_gate(step)
        if gate is not None:
            final = gate
            _emit({"type": "step", "url": getattr(page, "url", None), "step_id": step.step_id,
                   "action_type": step.action_type, "tier": None, "ms": 0.0, "outcome": final.status})
            break

        action_url = _resolve_value(step.value, params) if step.action_type == "navigate" else None
        t0 = time.monotonic()
        try:
            guardrail_check({"type": step.action_type, "url": action_url},
                             current_url=page.url, phase="replay")
        except GuardrailViolation as exc:
            final = _hard_failure(page, run_id, step.step_id, "action within allowlist", str(exc))
        except KeyError as exc:
            final = _hard_failure(page, run_id, step.step_id, "all required params provided", str(exc))
        else:
            final = _execute_step(step, page, params, tier_log, outputs, run_id, overrides)

        row = {
            "step_id": step.step_id, "action_type": step.action_type,
            "tier": _tier_for(step.step_id),
            "ms": round((time.monotonic() - t0) * 1000, 1),
            "outcome": final.status if final is not None else "ok",
        }
        trace.append(row)
        _emit({"type": "step", "url": getattr(page, "url", None), **row})
        if final is not None:
            break
    else:
        if not _verify_checkpoint(capability, page):
            final = _hard_failure(
                page, run_id, "checkpoint", capability.checkpoint.expected,
                f"checkpoint not satisfied at final url {page.url}",
            )
        else:
            final = Result(status="success", outputs=redact(outputs))

    final.trace = trace
    _write_trace(run_id, capability, params, trace, final)
    # Record a completed risky op so a retry under the same key won't re-execute it. A
    # hard_failure / data_unavailable is genuinely retryable, so it's not ledgered.
    if ledgered and idempotency_key and final.status in ("success", "business_outcome"):
        idempotency.record(idempotency_key, capability.capability_id, final.model_dump())
    _log.info("replay_done", run_id=run_id, status=final.status,
              code=final.business_outcome_code, tier_log=tier_log,
              total_ms=round(sum(r.get("ms", 0.0) for r in trace), 1))
    _emit({"type": "result", "status": final.status, "code": final.business_outcome_code,
           "outputs": final.outputs, "failure_detail": final.failure_detail,
           "total_ms": round(sum(r.get("ms", 0.0) for r in trace), 1)})
    return final


def replay(
    capability: Capability,
    params: dict,
    confirm: bool = False,
    headless: bool = True,
    run_id: str | None = None,
    idempotency_key: str | None = None,
    on_event=None,
    allow_escalation: bool = False,
    overrides: dict | None = None,
) -> Result:
    """Replay one capability, owning its own browser lifecycle. For a batch, prefer
    `replay.parallel.replay_many` - it reuses one warm browser across runs. `on_event` streams
    per-step + result dicts (see `_run_on_page`). `allow_escalation` = a live operator can
    approve a paused step (the web console sets this); without it a risky capability still needs
    `confirm=True` up front. `overrides` = {step_id: value} to replace recorded `type`/`select`
    values for this run (a different deposit amount, dispute reason, address …)."""
    run_id = run_id or f"replay_{int(time.time() * 1000)}"

    short, ledgered = _precheck(capability, confirm, idempotency_key, allow_escalation)
    if short is not None:
        if on_event is not None:
            try:
                on_event({"type": "result", "status": short.status,
                          "code": short.business_outcome_code, "failure_detail": short.failure_detail})
            except Exception:
                pass
        return short

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=LAUNCH_ARGS)
        page = browser.new_page()
        try:
            return _run_on_page(capability, params, page, run_id=run_id, ledgered=ledgered,
                                idempotency_key=idempotency_key, on_event=on_event,
                                confirm=confirm, allow_escalation=allow_escalation,
                                overrides=overrides)
        finally:
            if not headless:
                # headed run -- a human is presumably watching; leave the final page up.
                _log.info("headed_hold", run_id=run_id, seconds=5)
                time.sleep(5)
            browser.close()


class BrowserPool:
    """One warm Chromium, many isolated contexts. Reuses the browser across replays so a batch
    doesn't pay the ~0.5s `chromium.launch()` cost per run - each `run()` gets a fresh
    `new_context()` (its own cookies / storage), then the context is closed and the browser
    stays up.

    Single-threaded: Playwright's sync API isn't safe to share across threads, so a pool is
    driven from one thread. `replay_many(concurrency=1)` uses it; `concurrency>1` falls back to
    a browser-per-thread (see replay/parallel.py).
    """

    def __init__(self, headless: bool = True):
        self._headless = headless
        self._pw = None
        self._browser = None

    def __enter__(self) -> BrowserPool:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self._headless, args=LAUNCH_ARGS)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()

    def run(
        self, capability: Capability, params: dict, *,
        confirm: bool = False, run_id: str | None = None, idempotency_key: str | None = None,
        allow_escalation: bool = False, overrides: dict | None = None,
    ) -> Result:
        run_id = run_id or f"replay_{int(time.time() * 1000)}"
        short, ledgered = _precheck(capability, confirm, idempotency_key, allow_escalation)
        if short is not None:
            return short
        context = self._browser.new_context()
        try:
            return _run_on_page(capability, params, context.new_page(), run_id=run_id,
                                ledgered=ledgered, idempotency_key=idempotency_key,
                                confirm=confirm, allow_escalation=allow_escalation,
                                overrides=overrides)
        finally:
            context.close()
