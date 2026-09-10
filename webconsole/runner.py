"""
Drives one live run for the web console: opens a real browser, runs discovery or replay against
a chosen target, and pushes every agent event + a fresh screenshot onto queues the SSE endpoint
drains. One run at a time (a module lock); a run can be stopped mid-flight.

Playwright's sync API is single-threaded, so the whole run happens on one worker thread and the
screenshots are taken from inside the `on_event` hook (same thread as the page).
"""
from __future__ import annotations

import base64
import json
import queue
import threading
import time
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

import app_knowledge
from agent.compiler import CAPABILITIES_DIR as CAPS_DIR
from agent.compiler import compile_capability, save_capability
from agent.discovery import run_discovery
from agent_interface.runs import record_run
from artifact.schema import Capability, Checkpoint
from common.browser import LAUNCH_ARGS
from guardrails.policy import redact, redact_with_report
from replay.engine import _precheck, _run_on_page

_ASK_SYSTEM = (
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
_ASK_PHRASE_SYSTEM = (
    "Answer the user in one or two sentences, stating the concrete outcome: the value(s) "
    "returned, the business outcome (e.g. 'no such member', 'account locked'), or that the "
    "action was routed to a human for approval. Never claim success unless the result status "
    "is 'success'."
)
# The Chatbot's escape hatch: offered alongside the capability catalog so the model can say
# "none of these fit" and have a NEW capability learned for the request instead of failing.
_DISCOVER_TOOL = {
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

REPO = Path(__file__).parent.parent

_run_lock = threading.Lock()


def _unique_capability_id(base: str) -> str:
    """Keep the first discovery of a name clean; suffix later ones so nothing is overwritten
    and every discovered capability becomes its own replayable artifact."""
    base = (base or "discovered").strip().replace("/", "_") or "discovered"
    if not (CAPS_DIR / f"{base}.v1.json").exists():
        return base
    return f"{base}__{uuid.uuid4().hex[:6]}"


def _inferred_checkpoint(final_url: str, target_url: str, recorder=None,
                         transcript: list | None = None) -> Checkpoint:
    """Fallback when app_knowledge has no curated checkpoint. Preference order:

      1. `element_present` on the last extract step's role+name locator -- stable across
         differently-parameterized replays, and semantically "the datum we read is here".
      2. `url_match` on the last final-URL path segment that is NOT a parameter value: an
         all-digit segment, or a literal the agent typed/navigated with `param_name` set,
         is the member id itself (`/acct/12345`) and can't anchor a replay for another id.
      3. `url_match` on the entry URL (weak, but never false-fails a valid replay).

    Provenance is `proposed` -- this is the agent's inference, not ratified knowledge."""
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

    param_literals = set()
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


class LiveRun:
    """A single run. `events` is a queue of dicts; `.stop()` requests early termination; the run
    ends with a `{"type":"done", ...}` event."""

    def __init__(self):
        self.id = "live_" + uuid.uuid4().hex[:8]
        self.events: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self.started_at = time.time()
        self.status = "starting"
        self.escalations = 0  # counted from the event stream, recorded on the run

    # ---- lifecycle -----------------------------------------------------------------------

    def stop(self):
        self._stop.set()

    def _emit(self, ev: dict):
        ev.setdefault("ts", time.time())
        self.events.put(ev)

    def _shot(self, page):
        try:
            raw = page.screenshot(type="jpeg", quality=55, full_page=False)
            self._emit({"type": "frame", "b64": base64.b64encode(raw).decode(),
                        "url": getattr(page, "url", None)})
        except Exception:
            pass

    def _hook(self, page):
        """Returned callback: forwards an agent event to the client AND grabs a screenshot."""
        def cb(entry):
            e = dict(entry)
            if e.get("type") == "escalate_requested":
                self.escalations += 1
            # keep the stream light - drop the bulky raw accessibility tree
            e.pop("accessibility_tree", None)
            self._emit({"type": "agent", "entry": redact(e, sink="evidence")})
            self._shot(page)
            if self._stop.is_set():
                raise _Stopped()
        return cb

    def _redacted_outputs(self, outputs: dict, phase: str = "outputs") -> dict:
        """Redact a run's outputs for the `done` event and History, AND surface the
        `RedactionReport` on the stream - so the sink-aware redaction (guardrails/pii.py) is
        visible on EVERY run, not just discovery. `evidence` sink: tokenises names / emails /
        phones / addresses; ambiguous bare numerics are flagged, left intact."""
        safe, report, _ = redact_with_report(outputs or {}, sink="evidence")
        d = report.as_dict()
        if d["total_redacted"] or d["low_confidence_for_review"] or d["flagged_not_masked"]:
            self._emit({"type": "agent", "entry": {"type": "redaction", "phase": phase, **d}})
        return safe

    def _save_run(self, rec: dict):
        """Append this run to the History registry (agent_interface/runs.py). Best-effort -
        a registry write must never break the run itself."""
        try:
            record_run(self.id, rec.pop("kind"), rec.pop("capability_id"),
                       started_at=self.started_at, escalations=self.escalations, **rec)
        except Exception:
            pass

    def _run_capability(self, page, cap: Capability, params: dict, confirm: bool, overrides: dict):
        """Shared tail for replay and ask: precheck (with a live operator available) then run
        the deterministic engine on `page`, streaming every step + escalation to the client."""
        self.status = "running"
        # allow_escalation=True: a live operator is on the page, so a risky step pauses for
        # Approve/Decline (escalation/policy.py) instead of being refused up front.
        short, ledgered = _precheck(cap, confirm, None, allow_escalation=True)
        if short is not None:
            self._emit({"type": "agent", "entry": {"type": "precheck_block",
                        "detail": short.failure_detail}})
            return short
        return _run_on_page(cap, params, page, run_id=self.id, ledgered=ledgered,
                            idempotency_key=None, on_event=self._hook(page),
                            confirm=confirm, allow_escalation=True, overrides=overrides)

    def _discover_on_page(self, page, goal: str, target_url: str, capability_id: str,
                          app_name: str, api_key: str | None, provider: str | None):
        """Run the discovery loop on an already-open page; compile + save + emit `compiled` on
        success. Returns (DiscoveryResult, saved_relpath|None, resolved_capability_id). Used by
        both `run_discovery` and the Chatbot's discover-new-capability path."""
        capability_id = _unique_capability_id(capability_id)
        self._emit({"type": "meta", "mode": "discovery", "goal": goal,
                    "target": target_url, "capability_id": capability_id})
        self.status = "running"
        res = run_discovery(goal, target_url, page, api_key=api_key, provider=provider,
                            on_event=self._hook(page), max_steps=16, timeout_s=180,
                            capability_id=capability_id)
        self._shot(page)
        saved = None
        if res.status in ("success", "business_outcome"):
            cfg = app_knowledge.load(app_name).capability_config(capability_id)
            checkpoint = cfg.checkpoint or _inferred_checkpoint(
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
                saved = str(written.relative_to(REPO))       # tidy for the in-repo default
            except ValueError:
                saved = str(written)                         # CAPABILITIES_DIR is a mounted volume
            self._emit({"type": "compiled", "path": saved, "unratified": cap.unratified_rules()})
        return res, saved, capability_id

    # ---- the three run modes ----------------------------------------------------------------

    def run_replay(self, capability_path: str, params: dict, confirm: bool,
                   overrides: dict | None = None, inject: str | None = None):
        rec = {"kind": "replay", "capability_id": Path(capability_path).stem, "status": "error",
               "params": params, "outputs": {}, "business_outcome_code": None,
               "failure_detail": None, "extra": {"overrides": bool(overrides)}}
        if inject:
            rec["extra"]["inject"] = inject
        with sync_playwright() as p:
            browser = p.chromium.launch(args=LAUNCH_ARGS)
            page = browser.new_page()
            try:
                cap = Capability.model_validate_json(Path(capability_path).read_text())
                rec["capability_id"] = cap.capability_id
                if inject:
                    # append ?inject=<kind> to the entry navigation, exactly like MERIDIAN's
                    # per-request fault switch - the app carries it forward through the flow
                    for s in cap.steps:
                        if s.action_type == "navigate" and isinstance(s.value, str):
                            sep = "&" if "?" in s.value else "?"
                            s.value = f"{s.value}{sep}inject={inject}"
                            self._emit({"type": "agent", "entry": {"type": "inject",
                                        "kind": inject, "url": s.value}})
                            break
                self._emit({"type": "meta", "mode": "replay", "capability": cap.capability_id,
                            "risk": cap.risk_level, "lifecycle": cap.lifecycle, "params": params,
                            "overrides": overrides or {}, "confirm": confirm, "inject": inject})
                result = self._run_capability(page, cap, params, confirm, overrides or {})
                rec.update(status=result.status, outputs=result.outputs,
                           business_outcome_code=result.business_outcome_code,
                           failure_detail=result.failure_detail)
                self._shot(page)
                self._emit({"type": "done", "ok": True, "status": result.status,
                            "code": result.business_outcome_code,
                            "outputs": self._redacted_outputs(result.outputs),
                            "failure_detail": result.failure_detail})
            except _Stopped:
                rec["status"] = "stopped"
                self._emit({"type": "done", "ok": False, "status": "stopped"})
            except Exception as exc:  # surface, don't swallow
                rec.update(status="error", failure_detail={"error": f"{type(exc).__name__}: {exc}"})
                self._emit({"type": "done", "ok": False, "status": "error",
                            "detail": f"{type(exc).__name__}: {exc}"})
            finally:
                browser.close()
                self.status = "done"
                self._save_run(rec)

    def run_ask(self, request_text: str, api_key: str | None, provider: str | None,
                target_url: str, app_name: str):
        """Plain-English request -> the model picks one or more existing capabilities from the
        catalog (agent_interface/catalog.py) and each runs deterministically in order on the
        same page (a compound request like "name AND balance" needs two); OR, if nothing fits,
        it calls `discover_new_capability` and a NEW capability is learned (same discovery loop
        as Discover mode). Same escalation gate throughout, then one plain-language answer over
        all the results."""
        from agent.llm import make_session
        from agent_interface.catalog import build_tool_catalog, load_capabilities
        rec = {"kind": "ask", "capability_id": "-", "status": "error", "params": {},
               "outputs": {}, "business_outcome_code": None, "failure_detail": None,
               "extra": {"request": request_text}}
        with sync_playwright() as p:
            browser = p.chromium.launch(args=LAUNCH_ARGS)
            page = browser.new_page()
            try:
                self._emit({"type": "meta", "mode": "ask", "request": request_text})
                self.status = "running"
                session = make_session(api_key=api_key, provider=provider,
                                       tools=build_tool_catalog() + [_DISCOVER_TOOL])
                rec["extra"].update(provider=session.provider, model=session.model)
                self._emit({"type": "agent", "entry": {"type": "provider",
                            "provider": session.provider, "model": session.model}})
                session.add_user_text(request_text)
                turn = session.complete(_ASK_SYSTEM, 700, force_tool=False)
                caps = load_capabilities()

                # Each task the model chose, IN ORDER. A task with a matching capability runs
                # deterministically; one without triggers discovery (the LLM works it out and
                # a new capability is saved). Then on to the next task the same way. Stop at the
                # first that doesn't cleanly succeed.
                queue = [c for c in turn.tool_calls
                         if c.name in caps or c.name == "discover_new_capability"]
                if not queue:
                    why = (f"model picked unknown capability {turn.tool_calls[0].name!r}"
                           if turn.tool_calls else "no capability matched that request")
                    self._emit({"type": "agent", "entry": {"type": "stop", "reason": why}})
                    rec["status"] = "no_match"
                    self._emit({"type": "done", "ok": bool(turn.text), "status": "no_match",
                                "summary": turn.text or why, "detail": None if turn.text else why})
                    return

                merged: dict = {}
                ran: list[dict] = []
                learned: list[str] = []
                tool_results: list[tuple[str, str]] = []
                final_status, final_code, final_detail = "success", None, None
                for c in queue:
                    if c.name == "discover_new_capability":
                        goal = (c.input.get("goal") or request_text).strip()
                        name = (c.input.get("name") or "discovered").strip() or "discovered"
                        self._emit({"type": "agent", "entry": {"type": "tool_call",
                                    "name": "discover_new_capability",
                                    "input": {"goal": goal, "name": name}}})
                        res, saved, cid = self._discover_on_page(page, goal, target_url, name,
                                                                 app_name, api_key, provider)
                        outs, status, code, detail = res.outputs, res.status, res.business_outcome_code, None
                        ran.append({"task": f"discover:{cid}", "goal": goal, "status": status, "code": code})
                        if saved:
                            learned.append(saved)
                        tool_results.append((c.id, json.dumps({
                            "status": status, "business_outcome_code": code,
                            "outputs": redact(outs, sink="evidence"),
                            "learned_capability": saved}, default=str)))
                    else:
                        cap = caps[c.name]
                        args = {k: str(v) for k, v in (c.input or {}).items()}
                        self._emit({"type": "agent", "entry": {"type": "tool_call",
                                    "name": c.name, "input": args}})
                        self._emit({"type": "meta", "mode": "ask", "capability": c.name,
                                    "risk": cap.risk_level, "lifecycle": cap.lifecycle, "params": args})
                        result = self._run_capability(page, cap, args, False, {})
                        outs, status, code, detail = (result.outputs, result.status,
                                                      result.business_outcome_code, result.failure_detail)
                        ran.append({"task": c.name, "params": args, "status": status, "code": code})
                        tool_results.append((c.id, json.dumps({
                            "status": status, "business_outcome_code": code,
                            "outputs": redact(outs, sink="evidence")}, default=str)))
                    self._shot(page)
                    merged.update(outs or {})
                    if status != "success":
                        final_status, final_code, final_detail = status, code, detail
                        break

                # respond to any tool calls we didn't get to, so the phrasing call has a clean history
                done_ids = {c.id for c in queue[:len(ran)]}
                for c in turn.tool_calls:
                    if c.id not in done_ids:
                        tool_results.append((c.id, "skipped: not run this turn"))

                rec.update(capability_id=", ".join(x["task"] for x in ran),
                           params={"tasks": ran}, status=final_status, outputs=merged,
                           business_outcome_code=final_code, failure_detail=final_detail)
                rec["extra"]["picked"] = [x["task"] for x in ran]
                if learned:
                    rec["extra"]["learned"] = learned

                answer = ""
                try:  # one plain-language answer over every task's result - best-effort
                    session.add_tool_results(tool_results)
                    answer = session.complete(_ASK_PHRASE_SYSTEM, 280, force_tool=False).text
                except Exception:
                    pass
                if answer:
                    self._emit({"type": "agent", "entry": {"type": "answer", "text": answer}})
                done = {"type": "done", "ok": final_status == "success",
                        "status": final_status, "code": final_code,
                        "outputs": self._redacted_outputs(merged),
                        "failure_detail": final_detail, "summary": answer,
                        "capability_id": rec["capability_id"]}
                if learned:
                    done["capability_path"] = learned[-1]  # newest -> UI pulls it into Replay
                self._emit(done)
            except _Stopped:
                rec["status"] = "stopped"
                self._emit({"type": "done", "ok": False, "status": "stopped"})
            except Exception as exc:
                rec.update(status="error", failure_detail={"error": f"{type(exc).__name__}: {exc}"})
                self._emit({"type": "done", "ok": False, "status": "error",
                            "detail": f"{type(exc).__name__}: {exc}"})
            finally:
                browser.close()
                self.status = "done"
                self._save_run(rec)

    def run_discovery(self, goal: str, target_url: str, capability_id: str, app_name: str,
                      api_key: str | None, provider: str | None = None):
        rec = {"kind": "discovery", "capability_id": capability_id, "status": "error",
               "params": {"goal": goal}, "outputs": {}, "business_outcome_code": None,
               "failure_detail": None, "extra": {}}
        with sync_playwright() as p:
            browser = p.chromium.launch(args=LAUNCH_ARGS)
            page = browser.new_page()
            try:
                res, saved, cid = self._discover_on_page(page, goal, target_url, capability_id,
                                                         app_name, api_key, provider)
                rec.update(capability_id=cid, status=res.status, outputs=res.outputs,
                           business_outcome_code=res.business_outcome_code)
                rec["extra"] = {"tokens": res.token_usage, "capability_path": saved,
                                "provider": provider or "auto"}
                self._emit({"type": "done", "ok": True, "status": res.status,
                            "code": res.business_outcome_code, "summary": res.summary,
                            "outputs": self._redacted_outputs(res.outputs),
                            "tokens": res.token_usage, "capability_path": saved})
            except _Stopped:
                rec["status"] = "stopped"
                self._emit({"type": "done", "ok": False, "status": "stopped"})
            except Exception as exc:
                rec.update(status="error", failure_detail={"error": f"{type(exc).__name__}: {exc}"})
                self._emit({"type": "done", "ok": False, "status": "error",
                            "detail": f"{type(exc).__name__}: {exc}"})
            finally:
                browser.close()
                self.status = "done"
                self._save_run(rec)


class _Stopped(Exception):
    pass


# ---- registry ---------------------------------------------------------------------------------

_current: LiveRun | None = None


def current() -> LiveRun | None:
    return _current


def start(kind: str, **kw) -> LiveRun:
    """Start a run in a background thread. Raises RuntimeError if one is already active."""
    global _current
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError("a run is already in progress")
    run = LiveRun()
    _current = run

    def _target():
        try:
            if kind == "replay":
                run.run_replay(kw["capability_path"], kw["params"], kw["confirm"],
                               kw.get("overrides"), kw.get("inject"))
            elif kind == "ask":
                run.run_ask(kw["request"], kw.get("api_key"), kw.get("provider"),
                            kw["target_url"], kw["app_name"])
            else:
                run.run_discovery(kw["goal"], kw["target_url"], kw["capability_id"],
                                  kw["app_name"], kw.get("api_key"), kw.get("provider"))
        finally:
            _run_lock.release()

    threading.Thread(target=_target, name="liverun", daemon=True).start()
    return run


# ---- catalog for the UI --------------------------------------------------------------------

def catalog() -> dict:
    caps = []
    for path in sorted(CAPS_DIR.glob("*.json")):
        try:
            c = Capability.model_validate_json(path.read_text())
        except Exception:
            continue
        try:
            rel = str(path.relative_to(REPO))          # tidy for the in-repo default
        except ValueError:
            rel = str(path)                            # a mounted volume outside the repo
        # every type/select step that typed a fixed literal - the UI offers a per-run override
        # box for each so a replay can use a different deposit / reason / address / etc.
        overridable = [
            {"step_id": s.step_id,
             "field": (s.target.primary.get("name") if s.target else s.step_id),
             "value": s.value}
            for s in c.steps
            if s.action_type in ("type", "select") and isinstance(s.value, str)
        ]
        caps.append({"path": rel, "id": c.capability_id, "risk": c.risk_level,
                     "lifecycle": c.lifecycle, "app": c.target.app_name,
                     "inputs": sorted(c.input_schema.keys()),
                     "overridable": overridable})
    # safe capabilities first, the flagship lookup at the very top - a sensible default selection
    caps.sort(key=lambda x: (x["risk"] != "safe", x["id"] != "lookup_member_balance", x["id"]))
    return {"capabilities": caps}
