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
import re
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

# Hosts whose every route 302s to a login page without a session cookie — discovery must be
# pre-authenticated against these (see agent/session.py).
SESSION_REQUIRED_HOSTS = {"web-sample.interface-hiring.com"}


def _needs_session(target_url: str) -> bool:
    return urllib.parse.urlparse(target_url).netloc in SESSION_REQUIRED_HOSTS

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
    Fallback checkpoint for any capability_id not in CHECKPOINTS above (the earlier
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
    # Skip trailing segments that look like a per-run id (all digits, or a share/txn id like
    # 100234-S0001) — matching on those would make the checkpoint fail for any other
    # parameterised replay. Walk back to the last non-id segment (e.g. "members").
    def _looks_like_an_id(seg: str) -> bool:
        return seg.isdigit() or bool(re.match(r"^[0-9][0-9A-Za-z-]*$", seg))

    for seg in reversed(path_segments):
        if not _looks_like_an_id(seg):
            return Checkpoint(type="url_match", expected=seg)
    if path_segments:
        return Checkpoint(type="url_match", expected=path_segments[0])
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
    the same way a real operator's browser would be (the console requires auth)."""
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
    parser.add_argument("--meridian-session", action="store_true",
                         help="force pre-discovery operator sign-on (auto-enabled for a "
                              "web-sample.interface-hiring.com target)")
    parser.add_argument("--session-role", default="teller", choices=["teller", "supervisor"],
                         help="which operator role to sign on as for a session-gated target")
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

        # Targets that gate every route behind an operator sign-on (MERIDIAN CORE) need the
        # session established before discovery starts — otherwise every navigation 302s to the
        # login page. Pre-authenticate the SAME persistent context here by replaying the
        # meridian_signon capability, so the recorded capability never contains the login steps
        # or any credential; agent/session.py composes signon + capability again at replay time.
        if _needs_session(args.target) or args.meridian_session:
            from agent.session import credentials_for, load_signon_capability
            from replay.engine import replay as _replay
            role = args.session_role
            try:
                creds = credentials_for(role)
            except Exception as exc:
                print(f"ERROR: {args.target} needs an operator session but {exc}", file=sys.stderr)
                context.close()
                sys.exit(1)
            sres = _replay(load_signon_capability(), params=creds, page=page, run_id="discovery_signon")
            if sres.status != "success":
                print(f"ERROR: pre-discovery sign-on failed: {sres.failure_detail}", file=sys.stderr)
                context.close()
                sys.exit(1)
            print(f"[session] signed on as {role} ({creds['operator']}) — discovery starts authenticated")

        # Unless an operator console is actually wired up (--auto-approve / --open-console), a
        # dead-end or model escalation would otherwise block this unattended run forever. Cap the
        # wait so the run fails cleanly with status=escalation_timeout and still writes its
        # transcript.
        esc_wait = None if (args.auto_approve_escalation or args.open_console_on_escalation) else 150.0
        discovery_started = time.time()
        result = run_discovery(goal=args.goal, target_url=args.target, page=page,
                                max_steps=args.max_steps, escalation_max_wait_s=esc_wait)

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

        # the run registry the dashboard reads holds discovery AND replay runs (brief §3.4)
        from agent_interface.runs import record_run
        record_run(
            result.run_id, "discovery", args.capability_id,
            status=result.status, outputs=result.outputs,
            business_outcome_code=result.business_outcome_code,
            started_at=discovery_started,
            tier_log=result.recorder.tier_log if result.recorder else [],
            evidence_refs=[transcript_path.name]
            + [p.name for p in sorted(EVIDENCE_DIR.glob(f"*{result.run_id}*"))
               if p.name != transcript_path.name],
            extra={"goal": args.goal, "target": args.target},
        )

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

        if not args.headless:
            # headed run -- a human is presumably watching; leave the final page up for a few
            # seconds instead of yanking the window shut the instant the run finishes.
            print("Leaving the browser open for 5s so you can see the final state...")
            time.sleep(5)
        context.close()


if __name__ == "__main__":
    main()
