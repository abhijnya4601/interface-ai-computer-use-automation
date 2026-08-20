"""
Regression test for a real bug: the dead-end escalation path discarded the
`Lease` `trigger_escalation()` returned entirely, so whatever a human typed while resolving a
dead-end never reached the model on the next turn -- unlike the escalate() tool-call path, which
already threads `human_actions_summary` through. Found live: a user's human note explaining what
they'd done went nowhere, and the model had to guess at a confusing page state with zero context.

Drives the real loop (real browser, real Flask app, real escalation/controller.py -- only the
Anthropic client is faked, same pattern as smoke_test_discovery.py / smoke_test_escalation_timeout.py):
scripts 4 identical `type` calls (the search box value stops changing after the first, so 3
consecutive observations end up identical -- exactly what DEAD_END_REPEAT_THRESHOLD checks for),
a background thread resumes with a real human note once it sees the lease flip to "human", and
the test asserts that note actually appears in the next turn's `last_action_result` sent back to
the model.

Run: python scripts/smoke_test_dead_end_human_note.py   (needs the Flask app running on 5050)
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
HUMAN_NOTE = "I nudged the page for you -- try clicking Go now."


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


def _resume_with_note():
    """Stands in for a human resuming a dead-end with an explanatory note, not just a bare click."""
    deadline = time.time() + 30
    while time.time() < deadline:
        if controller.read_lease().state == "human":
            controller.signal_resume(human_actions_summary=HUMAN_NOTE)  # no decision -- dead-end, not approve/decline
            return
        time.sleep(0.2)
    raise SystemExit("dead-end escalation never triggered within 30s")


def main():
    if controller.LEASE_PATH.exists():
        controller.LEASE_PATH.unlink()
    if controller.RESUME_SIGNAL_PATH.exists():
        controller.RESUME_SIGNAL_PATH.unlink()

    # Same text typed repeatedly -> the search box's value stops changing after the first type,
    # so observations 2/3/4 hash identically -- exactly what triggers the dead-end detector.
    script = [
        {"name": "type", "input": {"role": "textbox", "name": "Search (ID / name)", "text": "12345"}},
        {"name": "type", "input": {"role": "textbox", "name": "Search (ID / name)", "text": "12345"}},
        {"name": "type", "input": {"role": "textbox", "name": "Search (ID / name)", "text": "12345"}},
        {"name": "type", "input": {"role": "textbox", "name": "Search (ID / name)", "text": "12345"}},
        {"name": "finish", "input": {
            "success": True, "outputs": {}, "summary": "resumed after dead-end and finished",
        }},
    ]
    fake_client = ScriptedAnthropicClient(script)

    import anthropic
    original_ctor = anthropic.Anthropic
    anthropic.Anthropic = lambda *a, **kw: fake_client

    resumer_thread = threading.Thread(target=_resume_with_note, daemon=True)
    resumer_thread.start()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            result = discovery_module.run_discovery(
                goal="Search for member 12345.",
                target_url=f"{BASE}/search",
                page=page,
                api_key="fake-key-not-used",
            )

            print(f"\nstatus={result.status} summary={result.summary!r}")

            dead_end_events = [e for e in result.transcript if e["type"] == "dead_end"]
            check("a dead-end was actually detected (proves this is a real test, not a no-op)",
                  len(dead_end_events) >= 1)

            resumed_events = [e for e in result.transcript if e["type"] == "escalation_resumed"]
            check("escalation_resumed was logged with the human's note, not discarded",
                  any(e.get("human_note") == HUMAN_NOTE for e in resumed_events))

            observation_events = [e for e in result.transcript if e["type"] == "observation"]
            check("the human's note actually reached the next turn's last_action_result "
                  "(what the model itself sees, not just the internal log)",
                  any(HUMAN_NOTE in (e.get("last_action_result") or "") for e in observation_events))

            check("run completed after the dead-end was resolved", result.status == "success")

            browser.close()
    finally:
        anthropic.Anthropic = original_ctor

    print("\nAll dead-end human-note regression checks passed.")


if __name__ == "__main__":
    main()
