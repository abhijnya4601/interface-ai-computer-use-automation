"""
Regression test for a real bug (DECISIONS.md D16): the discovery loop's wall-clock timeout used
to count time spent BLOCKED waiting for a human escalation decision against the same budget as
the agent's own working time. A human taking a few minutes to review and click Approve would
make an otherwise-successful run report status=timeout the instant it resumed.

This drives the real loop (real browser, real Flask app, real escalation/controller.py — only
the Anthropic client is faked, same pattern as smoke_test_discovery.py) with a deliberately
short timeout_s, and an escalation wait that's engineered to exceed it, to prove human review
time no longer burns the budget.

Run: python scripts/smoke_test_escalation_timeout.py   (needs the Flask app running on 5050)
"""
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

import agent.discovery as discovery_module
from escalation import controller

BASE = "http://localhost:5050"
SHORT_TIMEOUT_S = 3.0
ESCALATION_WAIT_S = 6.0  # deliberately longer than SHORT_TIMEOUT_S


@dataclass
class FakeBlock:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict = field(default_factory=dict)


@dataclass
class FakeResponse:
    content: list
    stop_reason: str = "tool_use"


class ScriptedAnthropicClient:
    def __init__(self, script: list[dict]):
        self._script = list(script)
        self._call_count = 0

        class _Messages:
            def create(inner_self, **kwargs):
                if not self._script:
                    raise AssertionError("scripted client ran out of canned tool calls")
                step = self._script.pop(0)
                self._call_count += 1
                block = FakeBlock(type="tool_use", id=f"toolu_fake_{self._call_count}",
                                   name=step["name"], input=step["input"])
                return FakeResponse(content=[block])

        self.messages = _Messages()


def check(label: str, condition: bool):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"Regression test failed: {label}")


def _delayed_approve():
    """Stands in for a slow human: waits longer than SHORT_TIMEOUT_S before resuming."""
    deadline = time.time() + 30
    while time.time() < deadline:
        if controller.read_lease().state == "human":
            time.sleep(ESCALATION_WAIT_S)
            controller.signal_resume(human_actions_summary="slow reviewer", decision="approved")
            return
        time.sleep(0.2)
    raise SystemExit("escalation never triggered within 30s")


def main():
    if controller.LEASE_PATH.exists():
        controller.LEASE_PATH.unlink()
    if controller.RESUME_SIGNAL_PATH.exists():
        controller.RESUME_SIGNAL_PATH.unlink()

    script = [
        {"name": "type", "input": {"role": "textbox", "name": "Search (ID / name)", "text": "12345"}},
        {"name": "click", "input": {"role": "button", "name": "Go"}},
        {"name": "escalate", "input": {"reason": "simulated risky step needing a slow human review"}},
        {"name": "finish", "input": {
            "success": True, "outputs": {}, "summary": "resumed after slow approval and finished",
        }},
    ]
    fake_client = ScriptedAnthropicClient(script)

    import anthropic
    original_ctor = anthropic.Anthropic
    anthropic.Anthropic = lambda *a, **kw: fake_client

    approver_thread = threading.Thread(target=_delayed_approve, daemon=True)
    approver_thread.start()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            start = time.monotonic()
            result = discovery_module.run_discovery(
                goal="Search for member 12345.",
                target_url=f"{BASE}/search",
                page=page,
                api_key="fake-key-not-used",
                timeout_s=SHORT_TIMEOUT_S,
            )
            elapsed = time.monotonic() - start

            print(f"\nelapsed wall time: {elapsed:.1f}s (escalation wait alone was "
                  f"{ESCALATION_WAIT_S}s, timeout_s was {SHORT_TIMEOUT_S}s)")
            print(f"status={result.status} summary={result.summary!r}")

            check("escalation wait exceeded the configured timeout_s (proves this is a real test)",
                  ESCALATION_WAIT_S > SHORT_TIMEOUT_S)
            check("run did NOT report status=timeout despite the long human wait",
                  result.status != "timeout")
            check("run completed successfully after the (slow) approval",
                  result.status == "success")

            browser.close()
    finally:
        anthropic.Anthropic = original_ctor

    print("\nAll escalation-timeout regression checks passed.")


if __name__ == "__main__":
    main()
