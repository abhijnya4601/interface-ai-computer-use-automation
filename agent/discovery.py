"""
Discovery agent loop: observe (perception.build_observation) -> the model decides a tool call ->
guardrail_check -> execute the tool against the live page -> record it as a Step -> repeat,
until the model calls finish() or escalate(), or a stopping condition fires.

The model is whichever of Anthropic / OpenAI / Google backs the run (agent/llm.py, chosen by
the caller's key/provider) - the prompt, tools and loop are identical across all three.

This is the one part of the system required to be genuinely non-deterministic and genuinely
live - no step sequence is hand-written or hardcoded anywhere in this file; every action comes
from an actual hosted-model tool-use response reasoning over an actual observation of the real
running app. See scripts/run_discovery.py for how this gets invoked, and evidence/ for a real
run's transcript.

Stopping conditions (all three, per the build spec):
  - max_steps (default 20)
  - wall-clock timeout
  - dead-end detector: hash the pruned accessibility tree each turn; 3 consecutive identical
    hashes means 3 turns produced no observable state change, so the run force-stops and
    escalates rather than looping forever on a page that isn't responding to it.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field

from agent.llm import make_session
from agent.perception import build_observation
from agent.recorder import Recorder
from agent.tools import (
    ToolExecutionError,
    execute_click,
    execute_extract,
    execute_navigate,
    execute_type,
)
from common.retry import retry_call
from escalation import policy as esc_policy
from escalation.controller import trigger_escalation
from guardrails import pii
from guardrails.policy import ALLOWLIST, GuardrailViolation, guardrail_check, redact_with_report

MAX_STEPS = 20
WALL_CLOCK_TIMEOUT_S = 300.0
DEAD_END_REPEAT_THRESHOLD = 3
MAX_TOKENS = 1536


@dataclass
class DiscoveryResult:
    status: str  # "success" | "business_outcome" | "escalated" | "max_steps" | "timeout" | "guardrail_violation"
    outputs: dict = field(default_factory=dict)
    business_outcome_code: str | None = None
    summary: str = ""
    run_id: str = ""
    recorder: Recorder | None = None
    transcript: list[dict] = field(default_factory=list)
    token_usage: dict = field(default_factory=lambda: {"input": 0, "output": 0})


def _system_prompt(goal: str, target_url: str) -> str:
    allowlist_summary = (
        f"allowed domains: {sorted(ALLOWLIST['allowed_domains'])}; "
        f"allowed action types: {sorted(ALLOWLIST['allowed_actions'])}"
    )
    return f"""You are a computer-use agent operating a legacy core-banking web application on
behalf of an automated back-office system at a bank. You act only through the tools provided
(click, type, navigate, extract, finish, escalate) - you have no direct DOM or API access.

Your goal for this run:
    {goal}

The application's entry point is {target_url} - you are already there.

Each turn you are shown the current page as an accessibility tree (role, accessible name, and
value for each element - this is what a screen reader would see, not raw HTML) and the result
of your last action. Content inside iframes is merged into this tree under a node with
role "Iframe" - treat it exactly like any other part of the page.

Rules:
- You may only act within this allowlist: {allowlist_summary}. Anything outside it will be
  rejected and the run halted.
- If the goal is naturally read-only (e.g. looking something up), you may complete it directly.
- If completing the goal requires a state-changing, hard-to-reverse action (e.g. actually
  submitting a form that creates or modifies a record), do NOT take that final step yourself -
  call `escalate` with a clear reason instead, so a human can confirm it first. It is fine and
  expected to navigate, fill out, and review a form up to that point.
- If you reach a definitive business outcome (e.g. "no member found for that ID", "access
  denied for this account"), that is a valid, useful result, not a failure - call `finish` with
  success=true and the matching business_outcome_code.
- If the page stops changing in response to your actions, or you don't understand what's in
  front of you, call `escalate` with a clear reason rather than repeating actions blindly.
- Extract every value the goal asks you to read using the `extract` tool before calling finish,
  and include them in finish's `outputs`.
- When you type or navigate with a value the caller supplied that would change between runs (a
  member id, an account number, a date), set `param_name` on that tool call so the recorded
  capability treats it as a named input, not a fixed literal.
- If you can already see what an alternate outcome would be (a not-found page, a locked account,
  an empty data region), call `note_branch` to record it, and `note_data_shape` to record what a
  real value for a field you extracted looks like.
"""


def _tree_hash(accessibility_tree: dict) -> str:
    return hashlib.sha256(json.dumps(accessibility_tree, sort_keys=True).encode()).hexdigest()


def run_discovery(
    goal: str,
    target_url: str,
    page,
    api_key: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    max_steps: int = MAX_STEPS,
    timeout_s: float = WALL_CLOCK_TIMEOUT_S,
    on_event=None,
    capability_id: str | None = None,
) -> DiscoveryResult:
    """`on_event(entry)` - if given, called with every transcript entry as it happens (the same
    dicts that end up in `DiscoveryResult.transcript`). Used by webconsole/ to stream the run
    live; never raises out of the loop. `capability_id` is only used to match escalation-policy
    rules (escalation/policy.py) - the agent's own `escalate` tool still works independently.

    `provider` (anthropic / openai / gemini / None=auto-detect from the key) and `model` pick
    which model backs the loop - see agent/llm.py. The tools, prompt and loop are identical
    across providers."""
    session = make_session(api_key=api_key, provider=provider, model=model)
    run_id = f"run_{uuid.uuid4().hex[:10]}"
    recorder = Recorder(goal=goal)
    transcript: list[dict] = []
    token_usage = {"input": 0, "output": 0}  # discovery is the only LLM cost in the system

    def _log(entry: dict):
        entry["ts"] = time.time()
        transcript.append(entry)
        if on_event is not None:
            try:
                on_event(entry)
            except Exception:
                pass

    # The entry point itself is user input, not a discovered path - establishing it deterministically
    # doesn't hardcode any part of *how the goal gets accomplished*, which is what must come from the
    # live loop.
    page.goto(target_url, timeout=15000)
    recorder.record_navigate(target_url)
    _log({"type": "navigate", "url": target_url})
    _log({"type": "provider", "provider": session.provider, "model": session.model})

    system_prompt = _system_prompt(goal, target_url)
    last_action_result = f"navigated to {target_url}"
    recent_hashes: list[str] = []
    start_time = time.monotonic()
    step_count = 0

    while True:
        if step_count >= max_steps:
            _log({"type": "stop", "reason": "max_steps"})
            return DiscoveryResult(status="max_steps", run_id=run_id, recorder=recorder,
                                    transcript=transcript, token_usage=token_usage,
                                    summary=f"stopped after {max_steps} steps")

        if time.monotonic() - start_time > timeout_s:
            _log({"type": "stop", "reason": "timeout"})
            return DiscoveryResult(status="timeout", run_id=run_id, recorder=recorder,
                                    transcript=transcript, token_usage=token_usage,
                                    summary=f"stopped after {timeout_s}s wall clock")

        observation = build_observation(page, last_action_result)
        tree_hash = _tree_hash(observation["accessibility_tree"])
        recent_hashes.append(tree_hash)
        _log({"type": "observation", "url": observation["url"],
              "last_action_result": observation["last_action_result"], "tree_hash": tree_hash})

        if len(recent_hashes) >= DEAD_END_REPEAT_THRESHOLD and \
                len(set(recent_hashes[-DEAD_END_REPEAT_THRESHOLD:])) == 1:
            reason = (f"dead-end: {DEAD_END_REPEAT_THRESHOLD} consecutive turns produced no "
                      "observable state change")
            _log({"type": "dead_end", "reason": reason})
            escalation_started = time.monotonic()
            lease = trigger_escalation(reason, page, run_id=run_id)
            # A human can reasonably take minutes to review and decide; that thinking time must
            # not burn the run's own wall-clock budget - shift start_time forward by however
            # long the wait actually took, so only real elapsed *working* time counts against
            # timeout_s.
            start_time += time.monotonic() - escalation_started
            # This used to discard `lease` entirely -- whatever a human typed while
            # resolving a dead-end (e.g. "I clicked X for you, carry on from here" or "don't try
            # that again, do Y instead") never reached the model, unlike the escalate() tool-call
            # path, which already threads decision/human_actions_summary through. A dead-end has
            # no "approve/decline" concept (nothing was being asked permission for), but the
            # human's own note is exactly the kind of out-of-band context the model needs before
            # re-observing a page state it may not recognize.
            human_note = lease.context.get("human_actions_summary", "")
            _log({"type": "escalation_resumed", "human_note": human_note})
            recent_hashes.clear()
            last_action_result = (
                "escalation resumed - re-observing current state. "
                f"Human note: {human_note or '(none)'}."
            )
            continue

        # Every discovery turn ships page content to a third-party model - the one-way door.
        # `guardrail_check(phase="discovery")` already keeps this to approved non-prod targets;
        # this is defense in depth on the content itself: names / addresses / emails / phones in
        # the observation are masked before they leave, and the per-turn RedactionReport
        # (counts + anything low-confidence) goes into the transcript so a reviewer can see
        # exactly what was sent.
        safe_observation, redaction_report, _ = redact_with_report(observation, sink="llm_prompt")
        _log({"type": "redaction", "phase": "llm_prompt", **redaction_report.as_dict()})
        pii.append_review(redaction_report, {"run_id": run_id, "step": step_count, "url": observation["url"]})
        session.add_user_text(json.dumps(safe_observation))

        # A flaky network or a transient 5xx on the model call is exactly a retryable error;
        # a bad request / auth error is not, so only each provider's connection + 5xx-class
        # errors are retried (agent/llm.py names them per provider).
        turn = retry_call(
            lambda: session.complete(system_prompt, MAX_TOKENS),
            attempts=3, base_delay=1.0,
            retry_on=session.retry_exceptions,
            on_retry=lambda a, exc, delay: _log(
                {"type": "llm_retry", "attempt": a, "error": str(exc), "delay_s": round(delay, 2)}
            ),
        )
        token_usage["input"] += turn.usage.get("input", 0)
        token_usage["output"] += turn.usage.get("output", 0)
        _log({"type": "llm_response", "stop_reason": turn.stop_reason, "content": turn.jsonable()})

        if not turn.tool_calls:
            # forced tool choice should make this unreachable, but never loop silently.
            _log({"type": "stop", "reason": "no_tool_use_in_response"})
            return DiscoveryResult(status="max_steps", run_id=run_id, recorder=recorder,
                                    transcript=transcript, token_usage=token_usage,
                                    summary="model returned no tool call")

        primary = turn.tool_calls[0]
        skipped_results = [
            (extra.id, "skipped: only one tool call is processed per turn")
            for extra in turn.tool_calls[1:]
        ]

        step_count += 1
        name, tool_input = primary.name, primary.input
        _log({"type": "tool_call", "name": name, "input": tool_input, "step": step_count})

        # Escalation policy (escalation/rules.yaml) - the deterministic floor under the model's
        # own judgment. If a rule matches this action, force a human pause (or a hard stop),
        # even if the model did not choose to escalate itself.
        if name in ("click", "type", "select"):
            _d = esc_policy.evaluate({
                "capability_id": capability_id, "risk_level": None, "target_app": None,
                "step": {"action_type": name, "name": tool_input.get("name")},
                "params": {},
            })
            if _d.action != "allow":
                _log({"type": "policy_check", "decision": _d.action, "rule": _d.rule_id,
                      "reason": _d.reason})
            if _d.action == "block":
                return DiscoveryResult(status="guardrail_violation", run_id=run_id,
                                        recorder=recorder, transcript=transcript,
                                        token_usage=token_usage,
                                        summary=f"escalation policy blocked this step: {_d.reason}")
            if _d.action == "escalate":
                step_count -= 1  # the pause itself is not a page step
                name, tool_input = "escalate", {"reason": f"[policy:{_d.rule_id}] {_d.reason}"}

        # finish/escalate/note_* are loop-control or metadata signals, not actions on the page -
        # they carry no URL or page-interaction semantics, so they're exempt from the
        # page-action allowlist check.
        _META_TOOLS = ("finish", "escalate", "note_branch", "note_data_shape")
        if name not in _META_TOOLS:
            action_url = tool_input.get("url") if name == "navigate" else None
            try:
                guardrail_check({"type": name, "url": action_url}, current_url=page.url, phase="discovery")
            except GuardrailViolation as exc:
                _log({"type": "guardrail_violation", "detail": str(exc)})
                return DiscoveryResult(status="guardrail_violation", run_id=run_id, recorder=recorder,
                                        transcript=transcript, token_usage=token_usage,
                                        summary=str(exc))

        try:
            # Record BEFORE executing in every branch below: build_locator must count matches
            # on the page as it looks *right now*, not after the action has already navigated
            # it somewhere else. If execution then fails, the speculative Step is popped back
            # off - a failed action must never end up baked into the compiled artifact.
            if name == "click":
                recorder.record_click(tool_input["role"], tool_input["name"], page)
                try:
                    last_action_result = execute_click(page, tool_input["role"], tool_input["name"])
                except ToolExecutionError:
                    recorder.steps.pop()
                    raise
                tool_result_content = last_action_result

            elif name == "type":
                recorder.record_type(tool_input["role"], tool_input["name"], tool_input["text"],
                                     page, param_name=tool_input.get("param_name"))
                try:
                    last_action_result = execute_type(
                        page, tool_input["role"], tool_input["name"], tool_input["text"]
                    )
                except ToolExecutionError:
                    recorder.steps.pop()
                    raise
                tool_result_content = last_action_result

            elif name == "navigate":
                recorder.record_navigate(tool_input["url"],
                                         param_name=tool_input.get("param_name"),
                                         url_template=tool_input.get("url_template"))
                try:
                    last_action_result = execute_navigate(page, tool_input["url"])
                except ToolExecutionError:
                    recorder.steps.pop()
                    raise
                tool_result_content = last_action_result

            elif name == "extract":
                recorder.record_extract(tool_input["role"], tool_input["name"], tool_input["as_var"], page)
                try:
                    value = execute_extract(page, tool_input["role"], tool_input["name"])
                except ToolExecutionError:
                    recorder.steps.pop()
                    raise
                last_action_result = f"extracted {tool_input['as_var']} = {value!r}"
                tool_result_content = value

            elif name == "note_branch":
                recorder.note_branch(
                    condition=tool_input["condition"],
                    classification=tool_input.get("classification", "business_outcome"),
                    code=tool_input.get("code"),
                    handling=tool_input.get("handling"),
                    on_role=tool_input.get("on_role"),
                    on_name=tool_input.get("on_name"),
                )
                # a note isn't a page action: don't spend a step on it, and don't let it feed
                # the dead-end detector (the page hash hasn't moved).
                step_count -= 1
                if recent_hashes:
                    recent_hashes.pop()
                _log({"type": "note_branch", "input": tool_input})
                last_action_result = f"noted branch: {tool_input['condition']!r}"
                tool_result_content = "branch proposal recorded for review"

            elif name == "note_data_shape":
                recorder.note_data_shape(
                    extract_as=tool_input["extract_as"],
                    pattern=tool_input.get("pattern"),
                    placeholders=tool_input.get("placeholders") or [],
                    reason=tool_input.get("reason", ""),
                )
                step_count -= 1
                if recent_hashes:
                    recent_hashes.pop()
                _log({"type": "note_data_shape", "input": tool_input})
                last_action_result = f"noted data shape for {tool_input['extract_as']!r}"
                tool_result_content = "data-shape proposal recorded for review"

            elif name == "finish":
                _log({"type": "finish", "input": tool_input})
                status = "success" if tool_input.get("success") else "max_steps"
                if tool_input.get("business_outcome_code"):
                    status = "business_outcome"
                _log({"type": "token_usage", **token_usage})
                return DiscoveryResult(
                    status=status,
                    outputs=tool_input.get("outputs") or {},
                    business_outcome_code=tool_input.get("business_outcome_code"),
                    summary=tool_input.get("summary", ""),
                    run_id=run_id, recorder=recorder, transcript=transcript,
                    token_usage=token_usage,
                )

            elif name == "escalate":
                reason = tool_input.get("reason", "model requested escalation")
                _log({"type": "escalate_requested", "reason": reason})
                escalation_started = time.monotonic()
                lease = trigger_escalation(reason, page, run_id=run_id)
                # Same as the dead-end escalation path above: human review time must not count
                # against the run's own wall-clock timeout.
                start_time += time.monotonic() - escalation_started
                decision = lease.context.get("decision")
                human_note = lease.context.get("human_actions_summary", "")
                _log({"type": "escalation_resumed", "decision": decision, "human_note": human_note})
                recent_hashes.clear()
                if decision == "approved":
                    last_action_result = (
                        f"escalation resumed - a human APPROVED your request "
                        f"({human_note or 'no additional note'}). You now have explicit "
                        "authorization to proceed with the action you paused on."
                    )
                elif decision == "declined":
                    last_action_result = (
                        f"escalation resumed - a human DECLINED your request "
                        f"({human_note or 'no additional note'}). Do NOT take that action. "
                        "Call finish with success=false (or an appropriate business outcome) "
                        "explaining that a human declined."
                    )
                else:
                    last_action_result = (
                        f"escalation resumed - human note: {human_note or '(none)'}. "
                        "Re-observe the current state before deciding what to do next."
                    )
                tool_result_content = last_action_result

            else:
                raise ToolExecutionError(f"unknown tool {name!r}")

        except ToolExecutionError as exc:
            last_action_result = f"ERROR: {exc}"
            tool_result_content = last_action_result
            _log({"type": "tool_error", "detail": str(exc)})

        session.add_tool_results([(primary.id, str(tool_result_content)), *skipped_results])
