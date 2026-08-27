"""
Fully-scripted MERIDIAN CORE escalation demo — real browser, a real separate operator-console
process, real HTTP, zero manual clicking. Shows the risky-action handoff on the replay path:

  invoke a risky capability without confirm
    -> replay walks the form, pauses before the irreversible click
    -> intervention request (capability, step, reason, URL, screenshot) -> operator console :5001
    -> a background watcher POSTs a real "Approve & Resume" (HTTP, Basic Auth)
    -> replay resumes, commits, returns success with the confirmation number

Then repeats with a "Decline" -> status=escalated, nothing committed.

Needs MERIDIAN_OPERATOR / MERIDIAN_PASSWORD.  Writes evidence/demo_meridian_escalation.json.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.session import run_with_session
from artifact.schema import Capability
from escalation import controller

REPO = Path(__file__).parent.parent
OPERATOR_BASE = "http://localhost:5001"
CAP = REPO / "capabilities" / "meridian_funds_transfer.v1.json"
EVIDENCE = REPO / "evidence" / "demo_meridian_escalation.json"


def _auth_header(user: str, pw: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def _decider(decision: str, headers: dict, stop: threading.Event) -> None:
    """Background: once the lease flips to human, wait a beat (a human reading the context) then
    POST the decision to the operator console over real HTTP."""
    while not stop.is_set():
        if controller.read_lease().state == "human":
            time.sleep(2)
            data = f"decision={decision}&summary=" + \
                   urllib.parse.quote(f"scripted demo {decision}")
            req = urllib.request.Request(f"{OPERATOR_BASE}/resume", data=data.encode(),
                                         method="POST", headers=headers)
            try:
                urllib.request.urlopen(req, timeout=10)
            except Exception as exc:
                print(f"[decider] POST /resume failed: {exc}", file=sys.stderr)
            return
        time.sleep(0.3)


def one_run(cap: Capability, params: dict, decision: str) -> dict:
    user, pw = "demo-bot", secrets.token_urlsafe(12)
    env = {**os.environ, "OPERATOR_USERNAME": user, "OPERATOR_PASSWORD": pw}
    for p in (controller.LEASE_PATH, controller.RESUME_SIGNAL_PATH):
        p.unlink(missing_ok=True)
    proc = subprocess.Popen([sys.executable, str(REPO / "escalation" / "operator_page.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    stop = threading.Event()
    threading.Thread(target=_decider, args=(decision, _auth_header(user, pw), stop),
                     daemon=True).start()
    try:
        result = run_with_session(cap, params, run_id=f"demo_esc_{decision}",
                                  risky_mode="escalate", escalation_max_wait_s=120)
    finally:
        stop.set()
        proc.terminate()
    print(f"  decision={decision!r:10} -> status={result.status} "
          f"outputs={result.outputs} detail={result.failure_detail}")
    return {"decision": decision, "status": result.status, "outputs": result.outputs,
            "failure_detail": result.failure_detail}


def main() -> int:
    if not os.environ.get("MERIDIAN_OPERATOR"):
        sys.exit("set MERIDIAN_OPERATOR / MERIDIAN_PASSWORD")
    cap = Capability.model_validate_json(CAP.read_text())
    # MMKT-10 has a large OPEN balance — a $1 demo transfer never runs it into a
    # HOLD/insufficient-funds business outcome before the escalation point.
    base = {"member_id": "100234", "from_share": "100234-MMKT-10",
            "to_share": "100234-S0001-11", "amount": "1.00", "memo": "escalation demo"}

    print("=== risky funds_transfer, operator APPROVES ===")
    approved = one_run(cap, {**base, "to_share": "100234-S0001-23"}, "approved")
    print("=== risky funds_transfer, operator DECLINES ===")
    declined = one_run(cap, base, "declined")

    EVIDENCE.write_text(json.dumps(
        {"approved": approved, "declined": declined, "at": time.time()}, indent=2))
    print(f"\nwrote {EVIDENCE}")
    ok = approved["status"] == "success" and declined["status"] == "escalated"
    print("DEMO OK" if ok else "DEMO FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
