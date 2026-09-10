"""
The assistant -- the reusable core that turns ONE plain-English request into a sequence of
tasks and carries each one out, in the order asked:

  * a task that matches a capability the system already has -> run it deterministically
    (replay engine, no model call)
  * a task with no capability -> discovery: the model works it out live and a new capability
    is compiled + saved, so the next request that needs it is free

Every task's structured output merges into one result and one plain-language answer.

`handle()` is provider-agnostic (agent/llm.py) and UI-agnostic: it owns the planning + phrasing
LLM calls and the reuse-vs-discover decision, and takes injected callbacks for "run a
capability" and "run discovery" plus an `on_event` sink, so it stays out of browser lifecycle,
escalation policy and event streaming. The console's Chatbot tab is one caller
(webconsole/runner.py); `run()` below is a headless one a CLI/API uses (scripts/assistant_cli.py).
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent.llm import make_session
from agent_interface.catalog import CAPABILITIES_DIR, build_tool_catalog, load_capabilities
from artifact.schema import Capability, Checkpoint, Result
from guardrails.policy import redact

_REPO = Path(__file__).parent.parent

PLAN_SYSTEM = (
    "You are a back-office assistant for a bank. Most tools run a pre-recorded, deterministic "
    "UI automation (a 'capability') against the servicing console and return a structured "
    "result. ALWAYS prefer existing tools: if one matches the user's request even loosely "
    "(e.g. a tool that reads the same field, for a different member), call it with the right "
    "typed arguments -- a tool named for member 12345 still works for any member id. If the "
    "request has several tasks (e.g. the name AND the balance), call one tool per task, in the "
    "order asked -- an existing tool where one fits, `discover_new_capability` (with a precise "
    "goal for just that task) where none does. You may mix both in one response. Only reply in "
    "plain text (no tool) if the request cannot be done against this app at all."
)
PHRASE_SYSTEM = (
    "Answer the user in one or two sentences, stating the concrete outcome: the value(s) "
    "returned, the business outcome (e.g. 'no such member', 'account locked'), or that the "
    "action was routed to a human for approval. Never claim success unless the result status "
    "is 'success'."
)
DISCOVER_TOOL = {
    "name": "discover_new_capability",
    "description": (
        "Use this ONLY when no other tool matches the request. Drives the app step by step to "
        "learn a NEW capability for it (a discovery run) - slower, calls the model repeatedly, "
        "and actually operates the site. The learned capability then becomes replayable."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "a precise, self-contained instruction "
                     "for the new task, including any ids/values from the request"},
            "name": {"type": "string", "description": "a short snake_case name for the new capability"},
        },
        "required": ["goal"],
    },
}

_noop: Callable[[dict], None] = lambda _e: None  # noqa: E731

# (goal, name) -> (DiscoveryResult, saved_path_or_none, resolved_capability_id)
Discover = Callable[[str, str], "tuple"]
RunCapability = Callable[[Capability, dict], Result]


@dataclass
class TaskRun:
    task: str                                  # capability id, or "discover:<id>"
    status: str
    params: dict = field(default_factory=dict)
    goal: str | None = None
    business_outcome_code: str | None = None


@dataclass
class AssistantResult:
    status: str                                # "success" | first non-success status | "no_match"
    outputs: dict = field(default_factory=dict)
    answer: str = ""
    tasks: list[TaskRun] = field(default_factory=list)
    learned: list[str] = field(default_factory=list)   # paths of newly compiled capabilities
    business_outcome_code: str | None = None
    failure_detail: dict | None = None
    plan_tokens: dict = field(default_factory=lambda: {"input": 0, "output": 0})


# --------------------------------------------------------------------------------------------
# the core
# --------------------------------------------------------------------------------------------

def handle(request: str, *, api_key: str | None, provider: str | None,
           run_capability: RunCapability, discover: Discover,
           on_event: Callable[[dict], None] = _noop,
           should_stop: Callable[[], bool] = lambda: False) -> AssistantResult:
    """Plan the request into tasks (one planning LLM call) and carry each out in order, then a
    single plain-language answer over every result. See module docstring for the callbacks."""
    session = make_session(api_key=api_key, provider=provider,
                           tools=build_tool_catalog() + [DISCOVER_TOOL])
    on_event({"type": "provider", "provider": session.provider, "model": session.model})
    session.add_user_text(request)
    turn = session.complete(PLAN_SYSTEM, 700, force_tool=False)
    caps = load_capabilities()

    queue = [c for c in turn.tool_calls
             if c.name in caps or c.name == "discover_new_capability"]
    if not queue:
        why = (f"model picked unknown capability {turn.tool_calls[0].name!r}"
               if turn.tool_calls else "no capability matched that request")
        on_event({"type": "stop", "reason": why})
        return AssistantResult(status="no_match", answer=turn.text or why, plan_tokens=turn.usage)

    merged: dict = {}
    tasks: list[TaskRun] = []
    learned: list[str] = []
    tool_results: list[tuple[str, str]] = []
    final_status, final_code, final_detail = "success", None, None

    for c in queue:
        if should_stop():
            break
        if c.name == "discover_new_capability":
            goal = (c.input.get("goal") or request).strip()
            name = (c.input.get("name") or "discovered").strip() or "discovered"
            on_event({"type": "tool_call", "name": "discover_new_capability",
                      "input": {"goal": goal, "name": name}})
            res, saved, cid = discover(goal, name)
            outs, status, code, detail = res.outputs, res.status, res.business_outcome_code, None
            tasks.append(TaskRun(task=f"discover:{cid}", goal=goal, status=status,
                                 business_outcome_code=code))
            if saved:
                learned.append(saved)
            tool_results.append((c.id, json.dumps({
                "status": status, "business_outcome_code": code,
                "outputs": redact(outs, sink="evidence"),
                "learned_capability": saved}, default=str)))
        else:
            cap = caps[c.name]
            args = {k: str(v) for k, v in (c.input or {}).items()}
            on_event({"type": "tool_call", "name": c.name, "input": args})
            on_event({"type": "task_meta", "capability": c.name, "risk": cap.risk_level,
                      "lifecycle": cap.lifecycle, "params": args})
            result = run_capability(cap, args)
            outs, status, code, detail = (result.outputs, result.status,
                                          result.business_outcome_code, result.failure_detail)
            tasks.append(TaskRun(task=c.name, params=args, status=status,
                                 business_outcome_code=code))
            tool_results.append((c.id, json.dumps({
                "status": status, "business_outcome_code": code,
                "outputs": redact(outs, sink="evidence")}, default=str)))
        merged.update(outs or {})
        if status != "success":
            final_status, final_code, final_detail = status, code, detail
            break

    done_ids = {c.id for c in queue[:len(tasks)]}
    for c in turn.tool_calls:
        if c.id not in done_ids:
            tool_results.append((c.id, "skipped: not run this turn"))

    answer = ""
    try:
        session.add_tool_results(tool_results)
        answer = session.complete(PHRASE_SYSTEM, 280, force_tool=False).text
    except Exception:
        pass
    if answer:
        on_event({"type": "answer", "text": answer})

    return AssistantResult(status=final_status, outputs=merged, answer=answer, tasks=tasks,
                           learned=learned, business_outcome_code=final_code,
                           failure_detail=final_detail, plan_tokens=turn.usage)


# --------------------------------------------------------------------------------------------
# discovery -> compile -> save  (shared by the console and the headless runner)
# --------------------------------------------------------------------------------------------

def unique_capability_id(base: str, caps_dir: Path | None = None) -> str:
    """Keep the first discovery of a name clean; suffix later ones so nothing is overwritten
    and every discovered capability becomes its own replayable artifact."""
    caps_dir = caps_dir or CAPABILITIES_DIR
    base = (base or "discovered").strip().replace("/", "_") or "discovered"
    if not (caps_dir / f"{base}.v1.json").exists():
        return base
    return f"{base}__{uuid.uuid4().hex[:6]}"


def infer_checkpoint(final_url: str, target_url: str, recorder=None,
                     transcript: list | None = None) -> Checkpoint:
    """Fallback when app_knowledge has no curated checkpoint. Preference order:

      1. `element_present` on the last extract step's role+name locator -- stable across
         differently-parameterized replays, and semantically "the datum we read is here".
      2. `url_match` on the last final-URL path segment that is NOT a parameter value: an
         all-digit segment, or a literal the agent typed/navigated with `param_name` set, is
         the member id itself (`/acct/12345`) and can't anchor a replay for another id.
      3. `url_match` on the entry URL (weak, but never false-fails a valid replay).

    Provenance is `proposed` -- the agent's inference, not ratified knowledge."""
    from urllib.parse import urlparse

    for s in reversed(list(getattr(recorder, "steps", []) or [])):
        if s.action_type == "extract" and s.target:
            role = s.target.primary.get("role")
            name = s.target.primary.get("name")
            if role and name:
                return Checkpoint(type="element_present", locator={"role": role, "name": name},
                                  expected="present", provenance="proposed")

    if final_url == target_url:
        return Checkpoint(type="url_match", expected=target_url, provenance="proposed")

    param_literals: set[str] = set()
    for e in (transcript or []):
        if e.get("type") == "tool_call" and e.get("input", {}).get("param_name"):
            for k in ("text", "url"):
                v = e["input"].get(k)
                if isinstance(v, str):
                    param_literals.add(v)
                    param_literals.update(p for p in v.split("/") if p)

    segs = [s for s in urlparse(final_url).path.split("/") if s]
    for seg in reversed(segs):
        if seg.isdigit() or seg in param_literals:
            continue
        return Checkpoint(type="url_match", expected=seg, provenance="proposed")
    return Checkpoint(type="url_match", expected=target_url, provenance="proposed")


def discover_and_compile(page, goal: str, capability_id: str, *, target_url: str, app_name: str,
                         api_key: str | None, provider: str | None,
                         on_step: Callable[[dict], None] = _noop,
                         on_compiled: Callable[[dict], None] = _noop):
    """Run the discovery loop on an already-open `page`, compile + save on success. Returns
    `(DiscoveryResult, saved_path_or_none, capability_id)`. `on_step` receives every transcript
    entry live; `on_compiled` gets `{"path", "unratified"}` once the artifact is written."""
    import app_knowledge
    from agent.compiler import compile_capability, save_capability
    from agent.discovery import run_discovery

    res = run_discovery(goal, target_url, page, api_key=api_key, provider=provider,
                        on_event=on_step, max_steps=16, timeout_s=180,
                        capability_id=capability_id)
    saved = None
    if res.status in ("success", "business_outcome"):
        cfg = app_knowledge.load(app_name).capability_config(capability_id)
        checkpoint = cfg.checkpoint or infer_checkpoint(
            page.url, target_url, res.recorder, res.transcript)
        risk = cfg.risk_level or (
            "risky" if any(e.get("type") == "escalate_requested" for e in res.transcript)
            else "safe")
        cap = compile_capability(
            capability_id=capability_id, version="1.0.0", run_id=res.run_id,
            target_url=target_url, risk_level=risk, recorder=res.recorder,
            outputs=res.outputs, checkpoint=checkpoint, description=goal, app_name=app_name)
        written = save_capability(cap)
        try:
            saved = str(written.relative_to(_REPO))   # tidy for the in-repo default
        except ValueError:
            saved = str(written)                      # CAPABILITIES_DIR is a mounted volume
        on_compiled({"path": saved, "unratified": cap.unratified_rules()})
    return res, saved, capability_id


# --------------------------------------------------------------------------------------------
# a headless caller (CLI / API): open one browser, wire deterministic replay + discovery
# --------------------------------------------------------------------------------------------

def run(request: str, *, target_url: str, app_name: str, api_key: str | None = None,
        provider: str | None = None, headless: bool = True, confirm: bool = False,
        on_event: Callable[[dict], None] = _noop) -> AssistantResult:
    """Same behaviour as the console's Chatbot tab, without a browser UI: one shared page for
    every task, deterministic replay for known capabilities, real discovery for the rest.
    A risky capability without `confirm=True` is refused (no interactive operator here)."""
    from playwright.sync_api import sync_playwright

    from common.browser import LAUNCH_ARGS
    from replay.engine import _precheck, _run_on_page

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=LAUNCH_ARGS)
        page = browser.new_page()
        try:
            def _run_cap(cap: Capability, args: dict) -> Result:
                short, ledgered = _precheck(cap, confirm, None, allow_escalation=False)
                if short is not None:
                    return short
                return _run_on_page(cap, args, page, run_id="assistant", ledgered=ledgered,
                                    idempotency_key=None, confirm=confirm, allow_escalation=False)

            def _discover(goal: str, name: str):
                cid = unique_capability_id(name)
                on_event({"type": "discovery_started", "goal": goal, "capability_id": cid})
                return discover_and_compile(
                    page, goal, cid, target_url=target_url, app_name=app_name,
                    api_key=api_key, provider=provider,
                    on_step=lambda e: on_event({"type": "agent", "entry": e}),
                    on_compiled=lambda d: on_event({"type": "compiled", **d}))

            return handle(request, api_key=api_key, provider=provider,
                          run_capability=_run_cap, discover=_discover, on_event=on_event)
        finally:
            browser.close()
