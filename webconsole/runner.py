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

import app_knowledge
from agent.compiler import CAPABILITIES_DIR as CAPS_DIR
from agent.compiler import compile_capability, save_capability
from agent.discovery import run_discovery
from artifact.schema import Capability, Checkpoint
from common.browser import LAUNCH_ARGS
from guardrails.policy import redact
from replay.engine import _precheck, _run_on_page

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
            # keep the stream light — drop the bulky raw accessibility tree
            e.pop("accessibility_tree", None)
            self._emit({"type": "agent", "entry": redact(e, sink="evidence")})
            self._shot(page)
            if self._stop.is_set():
                raise _Stopped()
        return cb

    # ---- the two run modes --------------------------------------------------------------

    def run_replay(self, capability_path: str, params: dict, confirm: bool,
                   overrides: dict | None = None):
        with sync_playwright() as p:
            browser = p.chromium.launch(args=LAUNCH_ARGS)
            page = browser.new_page()
            try:
                cap = Capability.model_validate_json(Path(capability_path).read_text())
                self._emit({"type": "meta", "mode": "replay", "capability": cap.capability_id,
                            "risk": cap.risk_level, "lifecycle": cap.lifecycle, "params": params,
                            "overrides": overrides or {}, "confirm": confirm})
                self.status = "running"
                # allow_escalation=True: a live operator is on the page, so a risky step pauses
                # for Approve/Decline (escalation/policy.py) instead of being refused up front.
                short, ledgered = _precheck(cap, confirm, None, allow_escalation=True)
                if short is not None:
                    self._emit({"type": "agent", "entry": {"type": "precheck_block",
                                "detail": short.failure_detail}})
                    result = short
                else:
                    result = _run_on_page(cap, params, page, run_id=self.id, ledgered=ledgered,
                                          idempotency_key=None, on_event=self._hook(page),
                                          confirm=confirm, allow_escalation=True,
                                          overrides=overrides or {})
                self._shot(page)
                self._emit({"type": "done", "ok": True, "status": result.status,
                            "code": result.business_outcome_code, "outputs": result.outputs,
                            "failure_detail": result.failure_detail})
            except _Stopped:
                self._emit({"type": "done", "ok": False, "status": "stopped"})
            except Exception as exc:  # surface, don't swallow
                self._emit({"type": "done", "ok": False, "status": "error",
                            "detail": f"{type(exc).__name__}: {exc}"})
            finally:
                browser.close()
                self.status = "done"

    def run_discovery(self, goal: str, target_url: str, capability_id: str, app_name: str,
                      api_key: str | None, provider: str | None = None):
        capability_id = _unique_capability_id(capability_id)
        with sync_playwright() as p:
            browser = p.chromium.launch(args=LAUNCH_ARGS)
            page = browser.new_page()
            try:
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
                        outputs=res.outputs, checkpoint=checkpoint, description=goal,
                        app_name=app_name)
                    saved = str(save_capability(cap).relative_to(REPO))
                    self._emit({"type": "compiled", "path": saved,
                                "unratified": cap.unratified_rules()})
                self._emit({"type": "done", "ok": True, "status": res.status,
                            "code": res.business_outcome_code, "summary": res.summary,
                            "outputs": redact(res.outputs, sink="evidence"),
                            "tokens": res.token_usage, "capability_path": saved})
            except _Stopped:
                self._emit({"type": "done", "ok": False, "status": "stopped"})
            except Exception as exc:
                self._emit({"type": "done", "ok": False, "status": "error",
                            "detail": f"{type(exc).__name__}: {exc}"})
            finally:
                browser.close()
                self.status = "done"


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
                               kw.get("overrides"))
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
        # every type/select step that typed a fixed literal — the UI offers a per-run override
        # box for each so a replay can use a different deposit / reason / address / etc.
        overridable = [
            {"step_id": s.step_id,
             "field": (s.target.primary.get("name") if s.target else s.step_id),
             "value": s.value}
            for s in c.steps
            if s.action_type in ("type", "select") and isinstance(s.value, str)
        ]
        caps.append({"path": rel, "id": c.capability_id, "risk": c.risk_level,
                     "lifecycle": c.lifecycle, "inputs": sorted(c.input_schema.keys()),
                     "overridable": overridable})
    # safe capabilities first, the flagship lookup at the very top — a sensible default selection
    caps.sort(key=lambda x: (x["risk"] != "safe", x["id"] != "lookup_member_balance", x["id"]))
    return {"capabilities": caps}
