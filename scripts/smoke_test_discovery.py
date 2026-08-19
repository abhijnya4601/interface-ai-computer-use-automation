"""
Pre-flight smoke test for agent/discovery.py's loop mechanics — NOT the required real discovery
run (that's scripts/run_discovery.py, with a real ANTHROPIC_API_KEY and a real Claude
tool-use response each turn). This script fakes the Anthropic client with a scripted sequence
of tool calls but drives everything else for real: a real Chromium, the real Flask app,
real guardrail_check calls, real Recorder/Step accumulation, real message-history bookkeeping.

Purpose: shake out plumbing bugs (JSON-serializing observations, tool_result formatting,
multi-turn message history, finish/extract handling) without spending real API credits, before
running the one genuinely LLM-driven run the assignment requires. If this script's assertions
pass, the loop's mechanics are trustworthy and the only remaining unknown for the real run is
Claude's own tool-use decisions.

Run: python scripts/smoke_test_discovery.py   (needs the Flask app running on 5050)
"""
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

import agent.discovery as discovery_module

BASE = "http://localhost:5050"


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
    """Stands in for anthropic.Anthropic — .messages.create() returns the next canned tool call."""

    def __init__(self, script: list[dict]):
        self._script = list(script)
        self._call_count = 0

        class _Messages:
            def create(inner_self, **kwargs):
                if not self._script:
                    raise AssertionError("scripted client ran out of canned tool calls")
                step = self._script.pop(0)
                self._call_count += 1
                block = FakeBlock(
                    type="tool_use", id=f"toolu_fake_{self._call_count}",
                    name=step["name"], input=step["input"],
                )
                return FakeResponse(content=[block])

        self.messages = _Messages()


def check(label: str, condition: bool):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"Smoke test failed: {label}")


def main():
    script = [
        {"name": "type", "input": {"role": "textbox", "name": "Search (ID / name)", "text": "12345"}},
        {"name": "click", "input": {"role": "button", "name": "Go"}},
        {"name": "click", "input": {"role": "link", "name": "View"}},
        {"name": "extract", "input": {"role": "rowheader", "name": "Savings Balance", "as_var": "savings_balance"}},
        {"name": "finish", "input": {
            "success": True, "outputs": {"savings_balance": "PLACEHOLDER"},
            "summary": "found member 12345 and read their savings balance",
        }},
    ]
    fake_client = ScriptedAnthropicClient(script)

    # monkeypatch: run_discovery builds its own anthropic.Anthropic() unless we bypass that.
    # simplest: patch the module-level anthropic.Anthropic constructor for this process.
    import anthropic
    original_ctor = anthropic.Anthropic
    anthropic.Anthropic = lambda *a, **kw: fake_client

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            result = discovery_module.run_discovery(
                goal="Look up member 12345 and read their current savings balance.",
                target_url=f"{BASE}/search",
                page=page,
                api_key="fake-key-not-used",
            )

            print(f"\nstatus={result.status} summary={result.summary!r} outputs={result.outputs}")
            check("status is success", result.status == "success")
            check("recorder captured 5 steps (1 auto-navigate + 4 recordable actions; "
                  "finish doesn't produce a Step)",
                  len(result.recorder.steps) == 5)
            check("a param_ref was detected for the typed member_id",
                  any(isinstance(s.value, dict) and s.value.get("param_ref") == "member_id"
                      for s in result.recorder.steps))
            check("tier log has one entry per located step",
                  len(result.recorder.tier_log) >= 4)
            check("transcript recorded llm_response entries",
                  any(e["type"] == "llm_response" for e in result.transcript))
            check("transcript recorded tool_call entries",
                  any(e["type"] == "tool_call" for e in result.transcript))

            browser.close()
    finally:
        anthropic.Anthropic = original_ctor

    print("\nAll discovery-loop smoke checks passed — plumbing is sound.")


if __name__ == "__main__":
    main()
