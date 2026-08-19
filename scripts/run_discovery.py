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
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

from agent.compiler import compile_capability, save_capability
from agent.discovery import run_discovery
from artifact.schema import Checkpoint
from guardrails.policy import redact

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"
USER_DATA_DIR = Path(__file__).parent.parent / ".playwright-profile"

CHECKPOINTS = {
    "lookup_member_balance": Checkpoint(
        type="element_present",
        locator={"role": "rowheader", "name": "Savings Balance"},
        expected="present",
    ),
    "open_subaccount": Checkpoint(
        type="text_match", locator=None, expected="Confirm and Open Account"
    ),
}
RISK_LEVELS = {"lookup_member_balance": "safe", "open_subaccount": "risky"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--capability-id", default="lookup_member_balance")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    EVIDENCE_DIR.mkdir(exist_ok=True)
    USER_DATA_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(USER_DATA_DIR), headless=args.headless)
        page = context.pages[0] if context.pages else context.new_page()

        result = run_discovery(goal=args.goal, target_url=args.target, page=page)

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
