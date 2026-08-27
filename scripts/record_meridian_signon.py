"""
Generate capabilities/meridian_signon.v1.json — the MERIDIAN CORE operator sign-on, one of the
target's §2.1 functions and the precondition every other capability needs.

Recorded against the live login form (real browser, no LLM: the flow is fixed and known —
operator ID, password, branch select, submit — so a scripted recorder run captures it exactly
and keeps the process reproducible), then the three credential fields are rewritten from the
literals used to record to {"param_ref": ...} so no credential is ever persisted in the
artifact. Credentials are supplied per invocation from the environment by agent/session.py.

Run:  python scripts/record_meridian_signon.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

from agent.compiler import compile_capability, save_capability
from agent.recorder import Recorder
from artifact.schema import Checkpoint

TARGET = "https://web-sample.interface-hiring.com/signon"
# literals used only to drive the recorder; swapped for param_refs before saving
_REC_OPERATOR, _REC_PASSWORD, _REC_BRANCH = "teller1", "password", "MAIN-001"
_PARAMS = {
    "operator": {"type": "string", "description": "MERIDIAN CORE operator ID to sign on with"},
    "password": {"type": "string", "description": "operator password (supplied from the environment, never stored)"},
    "branch": {"type": "string", "description": "branch code, e.g. MAIN-001"},
}
_LITERAL_TO_PARAM = {_REC_OPERATOR: "operator", _REC_PASSWORD: "password", _REC_BRANCH: "branch"}


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(TARGET, timeout=20000)

        rec = Recorder(goal="Sign on to MERIDIAN CORE with an operator ID, password and branch.")
        rec.record_navigate(TARGET)
        rec.record_type("textbox", "Operator ID", _REC_OPERATOR, page)
        rec.record_type("textbox", "Password", _REC_PASSWORD, page)
        rec.record_type("combobox", "Branch", _REC_BRANCH, page)
        rec.record_click("button", "Sign On", page)
        browser.close()

    capability = compile_capability(
        capability_id="meridian_signon",
        version="1.0.0",
        run_id="scripted_recorder",
        target_url=TARGET,
        risk_level="safe",
        recorder=rec,
        outputs={},
        checkpoint=Checkpoint(type="url_match", locator=None, expected="menu"),
        surface_type="legacy_web",
        description="Sign on to MERIDIAN CORE with an operator ID, password and branch.",
    )

    # rewrite the three credential literals -> param_refs, and declare the input schema
    for step in capability.steps:
        if isinstance(step.value, str) and step.value in _LITERAL_TO_PARAM:
            step.value = {"param_ref": _LITERAL_TO_PARAM[step.value]}
    capability.input_schema = _PARAMS

    path = save_capability(capability)
    print(f"wrote {path}")
    data = json.loads(Path(path).read_text())
    for s in data["steps"]:
        if s["action_type"] == "type":
            print(f"  {s['step_id']}: {s['target']['strategy']:<13} value={s['value']}")
    assert all(
        isinstance(s["value"], dict) and "param_ref" in s["value"]
        for s in data["steps"] if s["action_type"] == "type"
    ), "a credential literal survived into the artifact"
    print("OK — no credential literal in the artifact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
