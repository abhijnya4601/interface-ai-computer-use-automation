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
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
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


def _default_checkpoint(final_url: str, target_url: str) -> Checkpoint:
    """
    Fallback checkpoint for any capability_id not in CHECKPOINTS above (D22 — the earlier
    fallback, `Checkpoint(type="url_match", expected=target_url)`, checked whether the FINAL
    page was still the STARTING page, which is wrong for virtually every real capability, since
    the whole point of running one is to navigate somewhere else). Uses the final URL's last
    non-empty path segment instead of the full URL, since `url_match` is a substring check and
    the full path usually contains a per-run ID (e.g. `/member/12345/transactions`) that
    wouldn't match a differently-parameterized replay — the trailing route segment
    (`transactions`) is what's actually stable across runs.
    """
    if final_url == target_url:
        return Checkpoint(type="url_match", expected=target_url)
    path_segments = [seg for seg in urllib.parse.urlparse(final_url).path.split("/") if seg]
    if path_segments:
        return Checkpoint(type="url_match", expected=path_segments[-1])
    return Checkpoint(type="url_match", expected=final_url)


def _infer_risk_level(capability_id: str, transcript: list[dict]) -> str:
    """
    risk_level for any capability_id not in the RISK_LEVELS table above. A capability whose own
    discovery run needed a human to approve a state-changing step has no business defaulting to
    "safe" -- that default is exactly what would let replay execute it later with zero confirm
    gate. Found live: discovering `update_member_address` (never added to RISK_LEVELS) escalated
    mid-run for exactly this reason, and the unconditional `"safe"` default would have compiled
    it as risk_level=safe anyway.
    """
    if capability_id in RISK_LEVELS:
        return RISK_LEVELS[capability_id]
    escalated_mid_run = any(e.get("type") == "escalate_requested" for e in transcript)
    return "risky" if escalated_mid_run else "safe"


def _basic_auth_header(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _port_is_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _console_watcher_step(lease_state: str, already_opened: bool) -> tuple[bool, bool]:
    """
    Pure decision logic for _open_console_watcher, factored out so it's testable without real
    threads/sleeps: given the lease's current state and whether we've already opened the browser
    for the escalation currently in progress, returns (should_open_now, new_already_opened).
    Opens once per escalation, not once per poll tick, and re-arms after the lease resolves so a
    second escalation later in the same run reopens the console too.
    """
    if not already_opened and lease_state == "human":
        return True, True
    if already_opened and lease_state != "human":
        return False, False
    return False, already_opened


def _open_console_watcher(stop_event: threading.Event):
    """
    Background thread for --open-console-on-escalation: an operator who has to know the run
    escalated, remember the port, and go find it themselves is exactly the friction a real
    banker wouldn't tolerate (found live: a user asked "how would we know it's escalated and
    where to look" while genuinely trying to play the operator role). Pops the console straight
    into their default browser the instant the lease flips to human -- no run ID to hunt for,
    since read_lease() always shows whatever's currently pending, and there's only ever one.
    """
    already_opened = False
    while not stop_event.is_set():
        should_open, already_opened = _console_watcher_step(controller.read_lease().state, already_opened)
        if should_open:
            webbrowser.open(OPERATOR_BASE)
        time.sleep(0.3)


def _auto_approve_watcher(stop_event: threading.Event, auth_headers: dict):
    """Background thread: as soon as the lease flips to human, wait briefly (as a stand-in for
    a human actually reading the context) then post a real HTTP Approve & Resume, authenticated
    the same way a real operator's browser would be (D18 — the console requires auth)."""
    while not stop_event.is_set():
        if controller.read_lease().state == "human":
            time.sleep(2)  # stand-in for a human reading the escalation context
            data = "decision=approved&summary=" + urllib.parse.quote(
                "auto-approved by --auto-approve-escalation for an unattended recording run"
            )
            req = urllib.request.Request(
                f"{OPERATOR_BASE}/resume", data=data.encode(), method="POST", headers=auth_headers
            )
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
    parser.add_argument("--open-console-on-escalation", action="store_true",
                         help="if the run escalates, auto-open the operator console in your "
                              "browser the moment it happens, instead of you having to notice "
                              "the escalation and go find localhost:5001 yourself")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)
    if args.auto_approve_escalation and args.open_console_on_escalation:
        print("ERROR: --auto-approve-escalation and --open-console-on-escalation are "
              "contradictory (the first resolves the escalation before you'd ever see the "
              "console) -- pick one.", file=sys.stderr)
        sys.exit(1)

    EVIDENCE_DIR.mkdir(exist_ok=True)
    USER_DATA_DIR.mkdir(exist_ok=True)

    operator_proc = None
    stop_event = threading.Event()
    if args.open_console_on_escalation or args.auto_approve_escalation:
        if controller.LEASE_PATH.exists():
            controller.LEASE_PATH.unlink()
        if controller.RESUME_SIGNAL_PATH.exists():
            controller.RESUME_SIGNAL_PATH.unlink()
    if args.open_console_on_escalation:
        if _port_is_open("localhost", 5001):
            print("[open-console] localhost:5001 already has something listening -- using it "
                  "as-is, not starting a second operator console.")
        else:
            operator_env = dict(os.environ)
            operator_env.setdefault("OPERATOR_USERNAME", "banker")
            if not operator_env.get("OPERATOR_PASSWORD"):
                operator_env.pop("OPERATOR_PASSWORD", None)  # let operator_page.py generate+print one
            operator_proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).parent.parent / "escalation" / "operator_page.py")],
                env=operator_env,
            )
            time.sleep(1.5)
            print(f"[open-console] operator console starting at {OPERATOR_BASE} -- its login "
                  f"credentials are printed above (or in $OPERATOR_USERNAME/$OPERATOR_PASSWORD "
                  f"if you set them). Your browser will open there automatically if this run "
                  f"escalates.")
        threading.Thread(target=_open_console_watcher, args=(stop_event,), daemon=True).start()
    if args.auto_approve_escalation:
        # Generate the operator console's credential here and pass it to the subprocess via env
        # — this process and the watcher thread below share it directly, no parsing needed.
        operator_env = {
            **os.environ,
            "OPERATOR_USERNAME": "auto-approve-bot",
            "OPERATOR_PASSWORD": secrets.token_urlsafe(16),
        }
        operator_proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).parent.parent / "escalation" / "operator_page.py")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=operator_env,
        )
        time.sleep(1.5)
        auth_headers = _basic_auth_header(operator_env["OPERATOR_USERNAME"], operator_env["OPERATOR_PASSWORD"])
        threading.Thread(target=_auto_approve_watcher, args=(stop_event, auth_headers), daemon=True).start()

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
                args.capability_id, _default_checkpoint(page.url, args.target)
            )
            risk_level = _infer_risk_level(args.capability_id, result.transcript)
            capability = compile_capability(
                capability_id=args.capability_id,
                version=args.version,
                run_id=result.run_id,
                target_url=args.target,
                risk_level=risk_level,
                recorder=result.recorder,
                outputs=result.outputs,
                checkpoint=checkpoint,
                description=args.goal,
            )
            saved_path = save_capability(capability)
            print(f"capability saved to {saved_path}")
        else:
            print("run did not reach success/business_outcome; no capability compiled")

        context.close()


if __name__ == "__main__":
    main()
