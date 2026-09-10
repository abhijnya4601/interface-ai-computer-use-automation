"""
Drives one live run for the web console: opens a real browser, runs discovery or replay against
a chosen target, and pushes every agent event + a fresh screenshot onto queues the SSE endpoint
drains. One run at a time (a module lock); a run can be stopped mid-flight.

Playwright's sync API is single-threaded, so the whole run happens on one worker thread and the
screenshots are taken from inside the `on_event` hook (same thread as the page).
"""
from __future__ import annotations

import base64
import queue
import threading
import time
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

from agent_interface import assistant
from agent_interface.runs import record_run
from artifact.schema import Capability
from common.browser import LAUNCH_ARGS
from guardrails.policy import redact, redact_with_report
from replay.engine import _precheck, _run_on_page

REPO = Path(__file__).parent.parent
CAPS_DIR = assistant.CAPABILITIES_DIR

_run_lock = threading.Lock()


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
        """Streaming adapter around agent_interface.assistant.discover_and_compile: run discovery
        on an already-open page, compile + save, emit `meta` + `compiled`. Returns
        (DiscoveryResult, saved_path|None, resolved_capability_id). Used by both `run_discovery`
        and the Chatbot's discover-new-capability path."""
        cid = assistant.unique_capability_id(capability_id)
        self._emit({"type": "meta", "mode": "discovery", "goal": goal,
                    "target": target_url, "capability_id": cid})
        self.status = "running"
        res, saved, cid = assistant.discover_and_compile(
            page, goal, cid, target_url=target_url, app_name=app_name,
            api_key=api_key, provider=provider,
            on_step=self._hook(page),
            on_compiled=lambda d: self._emit({"type": "compiled", **d}))
        self._shot(page)
        return res, saved, cid

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
        """Streaming + History adapter around agent_interface.assistant.handle(): it plans the
        request into tasks and runs each in order -- an existing capability deterministically, a
        task with none via discovery -- then one plain-language answer over every result. This
        method only wires the assistant's callbacks to LiveRun's page, escalation gate and event
        queue; the orchestration lives in agent_interface/assistant.py so a CLI/API shares it."""
        rec = {"kind": "ask", "capability_id": "-", "status": "error", "params": {},
               "outputs": {}, "business_outcome_code": None, "failure_detail": None,
               "extra": {"request": request_text}}
        with sync_playwright() as p:
            browser = p.chromium.launch(args=LAUNCH_ARGS)
            page = browser.new_page()
            try:
                self._emit({"type": "meta", "mode": "ask", "request": request_text})
                self.status = "running"

                def _ev(e: dict):
                    t = e.get("type")
                    if t == "provider":
                        rec["extra"].update(provider=e.get("provider"), model=e.get("model"))
                        self._emit({"type": "agent", "entry": e})
                    elif t == "task_meta":
                        self._emit({"type": "meta", "mode": "ask",
                                    **{k: v for k, v in e.items() if k != "type"}})
                        self._shot(page)
                    else:  # tool_call / answer / stop
                        self._emit({"type": "agent", "entry": e})
                        if t == "tool_call":
                            self._shot(page)

                res = assistant.handle(
                    request_text, api_key=api_key, provider=provider,
                    run_capability=lambda cap, a: self._run_capability(page, cap, a, False, {}),
                    discover=lambda g, n: self._discover_on_page(
                        page, g, target_url, n, app_name, api_key, provider),
                    on_event=_ev, should_stop=self._stop.is_set)
                self._shot(page)

                if res.status == "no_match":
                    rec["status"] = "no_match"
                    self._emit({"type": "done", "ok": bool(res.answer), "status": "no_match",
                                "summary": res.answer,
                                "detail": None if res.answer else "no capability matched that request"})
                    return

                names = [t.task for t in res.tasks]
                rec.update(capability_id=", ".join(names) or "-",
                           params={"tasks": [vars(t) for t in res.tasks]},
                           status=res.status, outputs=res.outputs,
                           business_outcome_code=res.business_outcome_code,
                           failure_detail=res.failure_detail)
                rec["extra"]["picked"] = names
                if res.learned:
                    rec["extra"]["learned"] = res.learned
                done = {"type": "done", "ok": res.status == "success", "status": res.status,
                        "code": res.business_outcome_code,
                        "outputs": self._redacted_outputs(res.outputs),
                        "failure_detail": res.failure_detail, "summary": res.answer,
                        "capability_id": rec["capability_id"]}
                if res.learned:
                    done["capability_path"] = res.learned[-1]  # newest -> UI pulls it into Replay
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
