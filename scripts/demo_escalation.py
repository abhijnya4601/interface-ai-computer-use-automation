"""
Phase 7 live demo: force a real escalation during a real discovery run, confirm the real
operator console (a separate Flask process on :5001) shows the correct context, resume via a
real HTTP POST to it (exactly what a human clicking the page would trigger), and confirm the
discovery loop actually continues afterward using freshly re-perceived state. Every piece here
is real: real Chromium (persistent, non-headless-capable context), real Anthropic API calls,
real Flask operator app, real lease file, real blocking-poll-then-resume.

The goal below is deliberately impossible with the tools/UI available (no wire-transfer feature
exists) AND framed as risky/irreversible, so the discovery system prompt's own instruction
("do not take that final step yourself, call escalate") should make the model escalate itself
rather than hunt for a nonexistent button until the dead-end detector fires — both are legitimate
"real escalation" triggers per the assignment (dead-end is only the *example* given, not the only
valid path), and letting the model choose is more honest than scripting which one happens.

Run: python scripts/demo_escalation.py   (needs the Flask app running on 5050, ANTHROPIC_API_KEY set)
"""
import base64
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

from agent.discovery import run_discovery
from escalation import controller

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"
OPERATOR_BASE = "http://localhost:5001"
BANK_BASE = "http://localhost:5050"


def _basic_auth_header(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def main():
    EVIDENCE_DIR.mkdir(exist_ok=True)
    sequence = {"steps": []}

    def log(label, **data):
        entry = {"label": label, "ts": time.time(), **data}
        sequence["steps"].append(entry)
        print(f"\n=== {label} ===")
        print(json.dumps(data, indent=2, default=str)[:1500])

    # Clear any stale state from a previous run.
    controller.STATE_DIR.mkdir(exist_ok=True)
    if controller.LEASE_PATH.exists():
        controller.LEASE_PATH.unlink()
    if controller.RESUME_SIGNAL_PATH.exists():
        controller.RESUME_SIGNAL_PATH.unlink()

    operator_env = {
        **os.environ,
        "OPERATOR_USERNAME": "demo-script",
        "OPERATOR_PASSWORD": secrets.token_urlsafe(16),
    }
    auth_headers = _basic_auth_header(operator_env["OPERATOR_USERNAME"], operator_env["OPERATOR_PASSWORD"])
    operator_proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent.parent / "escalation" / "operator_page.py")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=operator_env,
    )
    time.sleep(1.5)
    log("operator_console_started", pid=operator_proc.pid, url=OPERATOR_BASE)

    result_holder = {}

    def _run():
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(Path(__file__).parent.parent / ".playwright-profile-escalation-demo"),
                headless=True,  # see DECISIONS.md: mechanism (lease flip, real resume, re-
                                 # observation) is demonstrated live; no display is available in
                                 # this environment for a literal visible takeover
            )
            page = context.pages[0] if context.pages else context.new_page()
            result = run_discovery(
                goal=("Open a new Christmas Club sub-account for member 12345 with a $50 "
                      "opening deposit, and complete the account creation."),
                target_url=f"{BANK_BASE}/search",
                page=page,
                max_steps=8,
            )
            result_holder["result"] = result
            context.close()

    thread = threading.Thread(target=_run)
    thread.start()

    # Poll for the lease to flip to "human" (i.e. trigger_escalation was called for real).
    deadline = time.time() + 90
    while time.time() < deadline:
        lease = controller.read_lease()
        if lease.state == "human":
            break
        time.sleep(0.5)
    else:
        thread.join(timeout=5)
        operator_proc.terminate()
        raise SystemExit("discovery run did not escalate within 90s — see result: "
                          f"{result_holder.get('result')}")

    log("escalation_triggered", lease_state=lease.state, context=lease.context)

    import urllib.parse
    import urllib.request
    get_req = urllib.request.Request(f"{OPERATOR_BASE}/", headers=auth_headers)
    with urllib.request.urlopen(get_req) as resp:
        operator_page_html = resp.read().decode()
    checks = {
        "shows_reason": lease.context.get("reason", "")[:30] in operator_page_html,
        "shows_current_url": lease.context.get("current_url", "") in operator_page_html,
        "has_resume_button": "Resume automation" in operator_page_html,
    }
    log("operator_page_fetched", url=f"{OPERATOR_BASE}/", checks=checks,
        html_excerpt=operator_page_html[:800])
    if not all(checks.values()):
        raise SystemExit(f"operator page did not show correct context: {checks}")

    # Simulate the human's action: a real HTTP POST to the real Resume route (exactly what
    # clicking the button in a browser would send).
    resume_data = "summary=" + urllib.parse.quote(
        "reviewed the request: no wire-transfer feature exists in this app; declined the "
        "transfer and resumed automation to let it report this cleanly"
    )
    req = urllib.request.Request(
        f"{OPERATOR_BASE}/resume", data=resume_data.encode(), method="POST", headers=auth_headers
    )
    urllib.request.urlopen(req)
    log("resume_posted_via_real_http", endpoint=f"{OPERATOR_BASE}/resume")

    thread.join(timeout=60)
    result = result_holder.get("result")
    log("discovery_resumed_and_finished",
        status=result.status if result else None,
        summary=result.summary if result else None,
        outputs=result.outputs if result else None)

    operator_proc.terminate()

    out_path = EVIDENCE_DIR / "escalation_demo_sequence.json"
    out_path.write_text(json.dumps(sequence, indent=2, default=str))
    print(f"\nfull sequence saved to {out_path}")


if __name__ == "__main__":
    main()
