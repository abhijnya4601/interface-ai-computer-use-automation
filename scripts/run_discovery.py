"""
CLI entrypoint for a real discovery run:

    python scripts/run_discovery.py \\
        --goal "Look up member 12345 and read their current savings balance." \\
        --target http://localhost:5050/search \\
        --capability-id lookup_member_balance

Requires ANTHROPIC_API_KEY and the mock app running (see README). Launches Playwright with a
PERSISTENT, non-headless context — not a throwaway one — because that same context is what a
human operator would take over during an escalation (see escalation/controller.py); running
headless here would make the "same live session" requirement a lie. Pass --headless only for
CI-style runs where no escalation/handoff demo is needed.

On success (or a business-outcome finish), compiles the recorded run into a Capability and
saves it under capabilities/. Always saves the full structured transcript to
evidence/discovery_<run_id>.jsonl, redacted, regardless of outcome.

--auto-approve-escalation starts the real escalation/operator_page.py console as a separate
process and, if the run escalates, posts a real HTTP "Approve & Resume" on the operator's
behalf after a short delay — this is what lets a capability whose goal requires a genuinely
irreversible final step (e.g. open_subaccount actually submitting) get recorded by one real,
unattended run instead of requiring someone to sit and click Resume by hand. Omit it for an
interactive session where you'll operate the console yourself.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

from agent.compiler import compile_capability, save_capability
from agent.discovery import run_discovery
from artifact.schema import Checkpoint
from escalation import controller
from guardrails.policy import redact

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"
USER_DATA_DIR = Path(__file__).parent.parent / ".playwright-profile"
OPERATOR_BASE = "http://localhost:5001"

CHECKPOINTS = {
    "lookup_member_balance": Checkpoint(
        type="element_present",
        locator={"role": "rowheader", "name": "Savings Balance"},
        expected="present",
    ),
    "open_subaccount": Checkpoint(
        type="text_match", locator=None, expected="created for"
    ),
}
RISK_LEVELS = {"lookup_member_balance": "safe", "open_subaccount": "risky"}


def _auto_approve_watcher(stop_event: threading.Event):
    """Background thread: as soon as the lease flips to human, wait briefly (as a stand-in for
    a human actually reading the context) then post a real HTTP Approve & Resume."""
    while not stop_event.is_set():
        if controller.read_lease().state == "human":
            time.sleep(2)  # stand-in for a human reading the escalation context
            data = "decision=approved&summary=" + urllib.parse.quote(
                "auto-approved by --auto-approve-escalation for an unattended recording run"
            )
            req = urllib.request.Request(f"{OPERATOR_BASE}/resume", data=data.encode(), method="POST")
            try:
                urllib.request.urlopen(req, timeout=5)
            except Exception as exc:
                print(f"[auto-approve] failed to POST resume: {exc}", file=sys.stderr)
            return
        time.sleep(0.3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--capability-id", default="lookup_member_balance")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--auto-approve-escalation", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    EVIDENCE_DIR.mkdir(exist_ok=True)
    USER_DATA_DIR.mkdir(exist_ok=True)

    operator_proc = None
    stop_event = threading.Event()
    if args.auto_approve_escalation:
        if controller.LEASE_PATH.exists():
            controller.LEASE_PATH.unlink()
        if controller.RESUME_SIGNAL_PATH.exists():
            controller.RESUME_SIGNAL_PATH.unlink()
        operator_proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).parent.parent / "escalation" / "operator_page.py")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        time.sleep(1.5)
        threading.Thread(target=_auto_approve_watcher, args=(stop_event,), daemon=True).start()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(USER_DATA_DIR), headless=args.headless)
        page = context.pages[0] if context.pages else context.new_page()

        result = run_discovery(goal=args.goal, target_url=args.target, page=page, max_steps=args.max_steps)

        stop_event.set()
        if operator_proc:
            operator_proc.terminate()

        print(f"\n=== discovery run {result.run_id} finished: status={result.status} ===")
        print(f"summary: {result.summary}")
        print(f"outputs: {result.outputs}")
        print(f"business_outcome_code: {result.business_outcome_code}")
        print(f"tier log: {result.recorder.tier_log}")

        transcript_path = EVIDENCE_DIR / f"discovery_{result.run_id}.jsonl"
        with open(transcript_path, "w") as f:
            for entry in result.transcript:
                f.write(json.dumps(redact(entry), default=str) + "\n")
        print(f"transcript saved to {transcript_path}")

        if result.status in ("success", "business_outcome"):
            checkpoint = CHECKPOINTS.get(
                args.capability_id, Checkpoint(type="url_match", expected=args.target)
            )
            risk_level = RISK_LEVELS.get(args.capability_id, "safe")
            capability = compile_capability(
                capability_id=args.capability_id,
                version=args.version,
                run_id=result.run_id,
                target_url=args.target,
                risk_level=risk_level,
                recorder=result.recorder,
                outputs=result.outputs,
                checkpoint=checkpoint,
            )
            saved_path = save_capability(capability)
            print(f"capability saved to {saved_path}")
        else:
            print("run did not reach success/business_outcome; no capability compiled")

        context.close()


if __name__ == "__main__":
    main()
