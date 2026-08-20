"""
Discovery agent loop: observe (perception.build_observation) -> Claude decides a tool call ->
guardrail_check -> execute the tool against the live page -> record it as a Step -> repeat,
until the model calls finish() or escalate(), or a stopping condition fires.

This is the one part of the system required to be genuinely non-deterministic and genuinely
live — no step sequence is hand-written or hardcoded anywhere in this file; every action comes
from an actual Anthropic API tool-use response reasoning over an actual observation of the real
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

import anthropic

from agent.perception import build_observation
from agent.recorder import Recorder
from agent.tools import TOOLS, ToolExecutionError, execute_click, execute_extract, execute_navigate, execute_type
from escalation.controller import trigger_escalation
from guardrails.policy import ALLOWLIST, GuardrailViolation, guardrail_check

DEFAULT_MODEL = "claude-sonnet-5"
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


def _system_prompt(goal: str, target_url: str) -> str:
    allowlist_summary = (
        f"allowed domains: {sorted(ALLOWLIST['allowed_domains'])}; "
        f"allowed action types: {sorted(ALLOWLIST['allowed_actions'])}"
    )
    return f"""You are a computer-use agent operating a legacy core-banking web application on
behalf of an automated back-office system at a bank. You act only through the tools provided
(click, type, navigate, extract, finish, escalate) — you have no direct DOM or API access.

Your goal for this run:
    {goal}

The application's entry point is {target_url} — you are already there.

Each turn you are shown the current page as an accessibility tree (role, accessible name, and
value for each element — this is what a screen reader would see, not raw HTML) and the result
of your last action. Content inside iframes is merged into this tree under a node with
role "Iframe" — treat it exactly like any other part of the page.

Rules:
- You may only act within this allowlist: {allowlist_summary}. Anything outside it will be
  rejected and the run halted.
- If the goal is naturally read-only (e.g. looking something up), you may complete it directly.
- If completing the goal requires a state-changing, hard-to-reverse action (e.g. actually
  submitting a form that creates or modifies a record), do NOT take that final step yourself —
  call `escalate` with a clear reason instead, so a human can confirm it first. It is fine and
  expected to navigate, fill out, and review a form up to that point.
- If you reach a definitive business outcome (e.g. "no member found for that ID", "access
  denied for this account"), that is a valid, useful result, not a failure — call `finish` with
  success=true and the matching business_outcome_code.
- If the page stops changing in response to your actions, or you don't understand what's in
  front of you, call `escalate` with a clear reason rather than repeating actions blindly.
- Extract every value the goal asks you to read using the `extract` tool before calling finish,
  and include them in finish's `outputs`.
"""


def _tree_hash(accessibility_tree: dict) -> str:
    return hashlib.sha256(json.dumps(accessibility_tree, sort_keys=True).encode()).hexdigest()


def _to_jsonable(content_blocks) -> list[dict]:
    out = []
    for block in content_blocks:
        if block.type == "text":
            out.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            out.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
    return out


def run_discovery(
    goal: str,
    target_url: str,
    page,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    max_steps: int = MAX_STEPS,
    timeout_s: float = WALL_CLOCK_TIMEOUT_S,
) -> DiscoveryResult:
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    run_id = f"run_{uuid.uuid4().hex[:10]}"
    recorder = Recorder(goal=goal)
    transcript: list[dict] = []

    def _log(entry: dict):
        entry["ts"] = time.time()
        transcript.append(entry)

    # The entry point itself is user input, not a discovered path — establishing it deterministically
    # doesn't hardcode any part of *how the goal gets accomplished*, which is what must come from the
    # live loop.
    page.goto(target_url, timeout=15000)
    recorder.record_navigate(target_url)
    _log({"type": "navigate", "url": target_url})

    system_prompt = _system_prompt(goal, target_url)
    messages: list[dict] = []
    last_action_result = f"navigated to {target_url}"
    recent_hashes: list[str] = []
    start_time = time.monotonic()
    step_count = 0

    while True:
        if step_count >= max_steps:
            _log({"type": "stop", "reason": "max_steps"})
            return DiscoveryResult(status="max_steps", run_id=run_id, recorder=recorder,
                                    transcript=transcript, summary=f"stopped after {max_steps} steps")

        if time.monotonic() - start_time > timeout_s:
            _log({"type": "stop", "reason": "timeout"})
            return DiscoveryResult(status="timeout", run_id=run_id, recorder=recorder,
                                    transcript=transcript, summary=f"stopped after {timeout_s}s wall clock")

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
            trigger_escalation(reason, page, run_id=run_id)
            # A human can reasonably take minutes to review and decide; that thinking time must
            # not burn the run's own wall-clock budget (DECISIONS.md D16) — shift start_time
            # forward by however long the wait actually took, so only real elapsed *working*
            # time counts against timeout_s.
            start_time += time.monotonic() - escalation_started
            _log({"type": "escalation_resumed"})
            recent_hashes.clear()
            last_action_result = "escalation resumed — re-observing current state"
            continue

        messages.append({"role": "user", "content": json.dumps(observation)})

        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            tools=TOOLS,
            tool_choice={"type": "any"},
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})
        _log({"type": "llm_response", "stop_reason": response.stop_reason,
              "content": _to_jsonable(response.content)})

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_use_blocks:
            # tool_choice="any" should make this unreachable, but never loop silently if it happens.
            _log({"type": "stop", "reason": "no_tool_use_in_response"})
            return DiscoveryResult(status="max_steps", run_id=run_id, recorder=recorder,
                                    transcript=transcript, summary="model returned no tool call")

        primary = tool_use_blocks[0]
        tool_results = []

        for extra in tool_use_blocks[1:]:
            tool_results.append({"type": "tool_result", "tool_use_id": extra.id,
                                  "content": "skipped: only one tool call is processed per turn"})

        step_count += 1
        name, tool_input = primary.name, primary.input
        _log({"type": "tool_call", "name": name, "input": tool_input, "step": step_count})

        # finish/escalate are loop-control signals, not actions on the page — they carry no URL
        # or page-interaction semantics, so they're exempt from the page-action allowlist check.
        if name not in ("finish", "escalate"):
            action_url = tool_input.get("url") if name == "navigate" else None
            try:
                guardrail_check({"type": name, "url": action_url}, current_url=page.url)
            except GuardrailViolation as exc:
                _log({"type": "guardrail_violation", "detail": str(exc)})
                return DiscoveryResult(status="guardrail_violation", run_id=run_id, recorder=recorder,
                                        transcript=transcript, summary=str(exc))

        try:
            # Record BEFORE executing in every branch below: build_locator must count matches
            # on the page as it looks *right now*, not after the action has already navigated
            # it somewhere else. If execution then fails, the speculative Step is popped back
            # off — a failed action must never end up baked into the compiled artifact.
            if name == "click":
                recorder.record_click(tool_input["role"], tool_input["name"], page)
                try:
                    last_action_result = execute_click(page, tool_input["role"], tool_input["name"])
                except ToolExecutionError:
                    recorder.steps.pop()
                    raise
                tool_result_content = last_action_result

            elif name == "type":
                recorder.record_type(tool_input["role"], tool_input["name"], tool_input["text"], page)
                try:
                    last_action_result = execute_type(
                        page, tool_input["role"], tool_input["name"], tool_input["text"]
                    )
                except ToolExecutionError:
                    recorder.steps.pop()
                    raise
                tool_result_content = last_action_result

            elif name == "navigate":
                recorder.record_navigate(tool_input["url"])
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

            elif name == "finish":
                _log({"type": "finish", "input": tool_input})
                status = "success" if tool_input.get("success") else "max_steps"
                if tool_input.get("business_outcome_code"):
                    status = "business_outcome"
                return DiscoveryResult(
                    status=status,
                    outputs=tool_input.get("outputs") or {},
                    business_outcome_code=tool_input.get("business_outcome_code"),
                    summary=tool_input.get("summary", ""),
                    run_id=run_id, recorder=recorder, transcript=transcript,
                )

            elif name == "escalate":
                reason = tool_input.get("reason", "model requested escalation")
                _log({"type": "escalate_requested", "reason": reason})
                escalation_started = time.monotonic()
                lease = trigger_escalation(reason, page, run_id=run_id)
                # See the dead-end escalation path above / DECISIONS.md D16: human review time
                # must not count against the run's own wall-clock timeout.
                start_time += time.monotonic() - escalation_started
                decision = lease.context.get("decision")
                human_note = lease.context.get("human_actions_summary", "")
                _log({"type": "escalation_resumed", "decision": decision, "human_note": human_note})
                recent_hashes.clear()
                if decision == "approved":
                    last_action_result = (
                        f"escalation resumed — a human APPROVED your request "
                        f"({human_note or 'no additional note'}). You now have explicit "
                        "authorization to proceed with the action you paused on."
                    )
                elif decision == "declined":
                    last_action_result = (
                        f"escalation resumed — a human DECLINED your request "
                        f"({human_note or 'no additional note'}). Do NOT take that action. "
                        "Call finish with success=false (or an appropriate business outcome) "
                        "explaining that a human declined."
                    )
                else:
                    last_action_result = (
                        f"escalation resumed — human note: {human_note or '(none)'}. "
                        "Re-observe the current state before deciding what to do next."
                    )
                tool_result_content = last_action_result

            else:
                raise ToolExecutionError(f"unknown tool {name!r}")

        except ToolExecutionError as exc:
            last_action_result = f"ERROR: {exc}"
            tool_result_content = last_action_result
            _log({"type": "tool_error", "detail": str(exc)})

        result_blocks = [{"type": "tool_result", "tool_use_id": primary.id, "content": str(tool_result_content)}]
        result_blocks.extend(tool_results)
        messages.append({"role": "user", "content": result_blocks})
