"""
Escalation & handoff controller.

Playwright must be launched with a persistent, non-headless context (see
scripts/run_discovery.py) so a human operator can take over the exact SAME live browser window
the automation was driving — this is what makes the "same live session" requirement literal
rather than simulated. This module implements the lease flip (automation -> human -> automation)
and blocks the calling discovery/replay loop until a human signals resume via the operator page.

Design: a small pair of files on disk (the lease itself, and a resume signal) polled at a short
interval — deliberately the least infrastructure that lets two separate local processes (the
automation loop, and escalation/operator_page.py's Flask app) coordinate on one piece of shared
state, per the assignment's explicit "don't build scaling infrastructure" guidance. A queue or a
socket would solve the same problem with more moving parts and nothing gained at this scale.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from escalation.lease import Lease
from guardrails.policy import redact

STATE_DIR = Path(__file__).parent / "state"
LEASE_PATH = STATE_DIR / "lease.json"
RESUME_SIGNAL_PATH = STATE_DIR / "resume.signal"
EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"

DEFAULT_POLL_INTERVAL_S = 1.0


def _write_lease(lease: Lease) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    LEASE_PATH.write_text(
        json.dumps({"state": lease.state, "context": redact(lease.context)}, indent=2)
    )


def read_lease() -> Lease:
    if not LEASE_PATH.exists():
        return Lease()
    data = json.loads(LEASE_PATH.read_text())
    return Lease(state=data.get("state", "automation"), context=data.get("context", {}))


def trigger_escalation(
    reason: str,
    page,
    run_id: str = "run",
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    max_wait_s: float | None = None,
) -> Lease:
    """
    Flips the lease to "human", captures a screenshot + context (current step, current URL, why
    it stopped), writes both to /evidence/, and BLOCKS — polling the resume-signal file — until
    a human resumes via the operator page (or, in tests, by calling signal_resume directly).
    Returns the fresh lease once resumed.
    """
    STATE_DIR.mkdir(exist_ok=True)
    EVIDENCE_DIR.mkdir(exist_ok=True)
    if RESUME_SIGNAL_PATH.exists():
        RESUME_SIGNAL_PATH.unlink()

    screenshot_path = EVIDENCE_DIR / f"escalation_{run_id}.png"
    try:
        page.screenshot(path=str(screenshot_path))
        screenshot_ref = str(screenshot_path)
    except Exception:
        screenshot_ref = None

    context = {
        "reason": reason,
        "current_url": getattr(page, "url", None),
        "screenshot_path": screenshot_ref,
        "run_id": run_id,
        "triggered_at": time.time(),
    }
    lease = Lease(state="human", context=context)
    _write_lease(lease)

    (EVIDENCE_DIR / f"escalation_{run_id}_context.json").write_text(
        json.dumps(redact(context), indent=2)
    )

    print(f"[escalation] ESCALATED: {reason}")
    print(
        f"[escalation] lease -> human. Waiting for operator to resume "
        f"(escalation/operator_page.py, or signal_resume() directly)."
    )

    waited = 0.0
    while not RESUME_SIGNAL_PATH.exists():
        time.sleep(poll_interval_s)
        waited += poll_interval_s
        if max_wait_s is not None and waited > max_wait_s:
            raise TimeoutError(f"escalation for run_id={run_id!r} not resumed within {max_wait_s}s")

    return resume()


def resume() -> Lease:
    """
    Flips the lease back to "automation" and clears the resume signal. Deliberately does NOT
    re-observe the page itself — callers (discovery.py / replay/engine.py) must call
    perception.build_observation() again after resume() returns, rather than reusing whatever
    they had cached before escalation, since the human may have changed the page state.

    The resume signal's `decision` and `human_actions_summary` (what the operator actually
    chose/did) are carried forward into the fresh lease's context, even though `state` is back
    to "automation" — this is the caller's only way to learn what the human decided, since the
    lease is the one piece of shared state both sides read. Without this, a resumed discovery
    loop has no way to tell "approved, proceed" apart from "declined, don't" (see DECISIONS.md
    D11/D12) and would have to guess.
    """
    decision = None
    human_actions_summary = ""
    if RESUME_SIGNAL_PATH.exists():
        try:
            signal_data = json.loads(RESUME_SIGNAL_PATH.read_text())
            decision = signal_data.get("decision")
            human_actions_summary = signal_data.get("human_actions_summary", "")
        except (json.JSONDecodeError, OSError):
            pass
        RESUME_SIGNAL_PATH.unlink()

    lease = Lease(
        state="automation",
        context={"decision": decision, "human_actions_summary": human_actions_summary},
    )
    _write_lease(lease)
    print(f"[escalation] lease -> automation (decision={decision!r}). "
          "Caller must re-observe before continuing.")
    return lease


def signal_resume(human_actions_summary: str = "", decision: str | None = None) -> None:
    """
    Called by the operator page (Phase 7) when a human clicks Resume. `decision` is one of
    "approved" (go ahead with whatever the agent was about to do), "declined" (don't — the
    agent should stop or find another path), or None (plain "I fixed something manually,
    continue" — the dead-end-recovery case, where approve/decline doesn't apply).
    """
    STATE_DIR.mkdir(exist_ok=True)
    RESUME_SIGNAL_PATH.write_text(
        json.dumps({
            "human_actions_summary": human_actions_summary,
            "decision": decision,
            "resumed_at": time.time(),
        })
    )
