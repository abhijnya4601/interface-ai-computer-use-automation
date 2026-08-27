"""
Generate capabilities/meridian_funds_transfer.v1.json — MERIDIAN CORE Funds Transfer
(from-share, to-share, amount, memo -> review -> post), one of the mandated §2.1 functions and
risk_level=risky (an irreversible ledger post).

Recorded with a scripted recorder rather than a live LLM discovery run, deliberately:
  - the flow is a fixed 5-field form -> Continue -> Post, with no branching to discover;
  - the mandated "check a member's balance" capability WAS produced by a real LLM discovery run
    (evidence/discovery_run_f9e05c9d33.jsonl) — that demonstrates the discovery loop;
  - a live transfer run repeatedly dead-ended on seed data where the first share is on HOLD and
    can't be debited (a real business outcome, now covered as one in the error taxonomy), which
    is a poor use of a risky irreversible POST during recording.
The recorded browser actions are real (this script drives a live session); only the decision of
*which* actions is scripted. The from/to/amount/memo literals used to record are then rewritten
to typed params, and the entry-point URL to a {member_id} template, so the capability is fully
parameterised.

Run (needs MERIDIAN_OPERATOR / MERIDIAN_PASSWORD):  python scripts/record_meridian_transfer.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

from agent.compiler import compile_capability, save_capability
from agent.recorder import Recorder
from agent.session import credentials_for, load_signon_capability
from agent.tools import execute_click, execute_type
from artifact.schema import Checkpoint
from replay.engine import replay

BASE = "https://web-sample.interface-hiring.com"
# recorded against member 100234 using two OPEN shares (the seed data's first shares are on HOLD)
_REC = {"member_id": "100234", "from_share": "100234-MMKT-13", "to_share": "100234-MMKT-14",
        "amount": "1.00", "memo": "demo transfer"}
_URL_TEMPLATE = BASE + "/members/{member_id}/transfer"
_PARAMS = {
    "member_id": {"type": "string", "description": "member number whose shares to move funds between"},
    "from_share": {"type": "string", "description": "source share id, e.g. 100234-MMKT-13 (must be OPEN, not HOLD)"},
    "to_share": {"type": "string", "description": "destination share id"},
    "amount": {"type": "string", "description": "amount to transfer, e.g. 1.00"},
    "memo": {"type": "string", "description": "free-text memo for the transfer"},
}
_LIT_TO_PARAM = {v: k for k, v in _REC.items() if k != "member_id"}


def main() -> int:
    creds = credentials_for("teller")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        replay(load_signon_capability(), params=creds, page=page, run_id="rec_signon")

        rec_url = _URL_TEMPLATE.format(**_REC)
        page.goto(rec_url, timeout=20000)

        rec = Recorder(goal="Transfer funds between two of a member's shares and post it.")
        rec.record_navigate(rec_url)
        for role, label, val in [
            ("combobox", "From Share", _REC["from_share"]),
            ("combobox", "To Share", _REC["to_share"]),
            ("textbox", "Amount", _REC["amount"]),
            ("textbox", "Memo", _REC["memo"]),
        ]:
            rec.record_type(role, label, val, page)
            execute_type(page, role, label, val)
        rec.record_click("button", "Continue", page)
        execute_click(page, "button", "Continue")
        page.wait_for_load_state()
        rec.record_click("button", "Post Transfer", page)
        execute_click(page, "button", "Post Transfer")
        page.wait_for_load_state()
        rec.record_extract("cell", "Confirmation:", "confirmation", page)
        browser.close()

    capability = compile_capability(
        capability_id="meridian_funds_transfer",
        version="1.0.0",
        run_id="scripted_recorder",
        target_url=_URL_TEMPLATE,
        risk_level="risky",
        recorder=rec,
        outputs={"confirmation": ""},
        checkpoint=Checkpoint(type="text_match", locator=None, expected="TRANSFER POSTED"),
        surface_type="legacy_web",
        description="Transfer funds from one of a member's shares to another and post the transfer.",
        app_name="meridian-core",
    )
    # parameterise: the entry-point URL -> {member_id} template; from/to/amount/memo literals -> params
    capability.steps[0].value = _URL_TEMPLATE
    for step in capability.steps:
        if isinstance(step.value, str) and step.value in _LIT_TO_PARAM:
            step.value = {"param_ref": _LIT_TO_PARAM[step.value]}
    capability.input_schema = _PARAMS

    path = save_capability(capability)
    print(f"wrote {path}")
    data = json.loads(Path(path).read_text())
    for s in data["steps"]:
        t = s.get("target") or {}
        print(f"  {s['step_id']:3} {s['action_type']:8} {t.get('strategy','-'):14} "
              f"{t.get('primary')} val={s.get('value')} as={s.get('extract_as')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
