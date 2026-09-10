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
    "result. Choose the one tool and the typed arguments that match the user's request. If "
    "none of the pre-recorded tools fit, call `discover_new_capability` with a precise goal so "
    "a new one is learned for this request. Only reply in plain text (no tool) if the request "
    "cannot be done against this app at all."
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


def _inferred_checkpoint(final_url: str, target_url: str) -> Checkpoint:
    """Fallback when app_knowledge has no curated checkpoint: the final URL's last path segment
    (stable across differently-parameterized replays), or the whole URL if it never navigated."""
    from urllib.parse import urlparse
    if final_url == target_url:
        return Checkpoint(type="url_match", expected=target_url)
    segs = [s for s in urlparse(final_url).path.split("/") if s]
    return Checkpoint(type="url_match", expected=segs[-1] if segs else final_url)


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
            checkpoint = cfg.checkpoint or _inferred_checkpoint(page.url, target_url)
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
        """Plain-English request -> the model either picks one existing capability + typed args
        from the catalog (agent_interface/catalog.py) and runs it deterministically, OR, if
        nothing fits, calls `discover_new_capability` and a NEW capability is learned for the
        request (same discovery loop as Discover mode). Same escalation gate throughout, then a
        one-sentence answer."""
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
                turn = session.complete(_ASK_SYSTEM, 500, force_tool=False)
                caps = load_capabilities()
                call = turn.tool_calls[0] if turn.tool_calls else None

                if call is not None and call.name == "discover_new_capability":
                    goal = (call.input.get("goal") or request_text).strip()
                    name = (call.input.get("name") or "discovered").strip() or "discovered"
                    self._emit({"type": "agent", "entry": {"type": "tool_call",
                                "name": "discover_new_capability", "input": {"goal": goal, "name": name}}})
                    res, saved, cid = self._discover_on_page(page, goal, target_url, name,
                                                             app_name, api_key, provider)
                    rec.update(capability_id=cid, params={"goal": goal}, status=res.status,
                               outputs=res.outputs, business_outcome_code=res.business_outcome_code)
                    rec["extra"].update(picked="discover_new_capability",
                                        capability_path=saved, tokens=res.token_usage)
                    self._emit({"type": "done", "ok": res.status in ("success", "business_outcome"),
                                "status": res.status, "code": res.business_outcome_code,
                                "summary": res.summary,
                                "outputs": self._redacted_outputs(res.outputs),
                                "capability_path": saved})
                    return

                if call is None or call.name not in caps:
                    why = (f"model picked unknown capability {call.name!r}" if call
                           else "no capability matched that request")
                    self._emit({"type": "agent", "entry": {"type": "stop", "reason": why}})
                    rec["status"] = "no_match"
                    self._emit({"type": "done", "ok": bool(turn.text), "status": "no_match",
                                "summary": turn.text or why, "detail": None if turn.text else why})
                    return
                cap = caps[call.name]
                args = {k: str(v) for k, v in (call.input or {}).items()}
                rec.update(capability_id=call.name, params=args)
                rec["extra"]["picked"] = call.name
                self._emit({"type": "agent", "entry": {"type": "tool_call",
                            "name": call.name, "input": args}})
                self._emit({"type": "meta", "mode": "ask", "capability": call.name,
                            "risk": cap.risk_level, "lifecycle": cap.lifecycle, "params": args})
                result = self._run_capability(page, cap, args, False, {})
                rec.update(status=result.status, outputs=result.outputs,
                           business_outcome_code=result.business_outcome_code,
                           failure_detail=result.failure_detail)
                self._shot(page)
                answer = ""
                try:  # plain-language phrasing - best-effort, non-forced tool choice
                    session.add_tool_results([(call.id, json.dumps({
                        "status": result.status,
                        "business_outcome_code": result.business_outcome_code,
                        "outputs": redact(result.outputs, sink="evidence")}, default=str))])
                    answer = session.complete(_ASK_PHRASE_SYSTEM, 220, force_tool=False).text
                except Exception:
                    pass
                if answer:
                    self._emit({"type": "agent", "entry": {"type": "answer", "text": answer}})
                self._emit({"type": "done", "ok": True, "status": result.status,
                            "code": result.business_outcome_code,
                            "outputs": self._redacted_outputs(result.outputs),
                            "failure_detail": result.failure_detail, "summary": answer,
                            "capability_id": call.name})
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
