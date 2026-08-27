"""
Record the remaining fixed-form MERIDIAN CORE §2.1 capabilities with a scripted recorder, the
same deliberate call as meridian_signon / meridian_funds_transfer: each is a
non-branching form -> [review ->] post, the discovery loop is already demonstrated by
meridian_check_member_balance (a real LLM run), and driving an irreversible POST with the LLM
just to record it is a poor trade. The browser actions are real; only *which* actions is
scripted. Field literals are rewritten to typed params and the entry URL to a {member_id}
template before saving.

    python scripts/record_meridian_flow.py open_share
    python scripts/record_meridian_flow.py update_info
    python scripts/record_meridian_flow.py place_hold      # recorded as super1
    python scripts/record_meridian_flow.py all
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

FLOWS = {
    "open_share": dict(
        capability_id="meridian_open_share",
        url_template=BASE + "/members/{member_id}/open-share",
        role="teller",
        risk_level="risky",
        requires_role=None,
        description="Open a new share of a given type for a member with an initial deposit.",
        rec_params={"member_id": "100234", "share_type": "MMKT", "deposit": "5.00"},
        fields=[("combobox", "Share Type", "share_type"), ("textbox", "Initial Deposit", "deposit")],
        clicks=["Continue", "Open Share"],
        extract=("Confirmation", "confirmation"),
        checkpoint_text="SHARE OPENED",
        param_schema={
            "member_id": "member number to open the share for",
            "share_type": "share type code: S0001 (Regular), S0070 (Checking), MMKT, or CERT",
            "deposit": "initial deposit amount, e.g. 5.00",
        },
    ),
    "update_info": dict(
        capability_id="meridian_update_info",
        url_template=BASE + "/members/{member_id}/update",
        role="teller",
        risk_level="risky",
        requires_role=None,
        description="Update a member's e-mail, phone and mailing address.",
        rec_params={"member_id": "100234", "email": "ada.recon@example.com",
                    "phone": "555-0199", "address": "99 Recon Ave"},
        fields=[("textbox", "E-mail", "email"), ("textbox", "Phone", "phone"),
                ("textbox", "Mailing Address", "address")],
        clicks=["Save Changes"],
        extract=None,
        checkpoint_text="INFORMATION UPDATED",
        param_schema={
            "member_id": "member number to update",
            "email": "new e-mail address",
            "phone": "new phone number",
            "address": "new mailing address",
        },
    ),
    "inquiry_by_name": dict(
        capability_id="meridian_member_inquiry_by_name",
        url_template=BASE + "/members",
        role="teller",
        risk_level="safe",
        requires_role=None,
        description="Find a member by last name and return the first match's number and name.",
        rec_params={"last_name": "Lovelace"},
        fields=[("combobox", "Search by", "__literal__:Last Name"),
                ("textbox", "Value", "last_name")],
        clicks=["Search"],
        extract=None,
        extracts=[("cell", "100234", "member_no"), ("cell", "Lovelace, Ada", "member_name")],
        checkpoint_text="Select",
        param_schema={"last_name": "member's last name to search for"},
    ),
    "place_hold": dict(
        capability_id="meridian_place_hold",
        url_template=BASE + "/members/{member_id}/hold",
        role="supervisor",
        risk_level="risky",
        requires_role="supervisor",
        description="Place a hold on one of a member's shares (reason code + notes). Supervisor only.",
        rec_params={"member_id": "100234", "share": "100234-MMKT-13",
                    "reason": "FRAUD", "notes": "recorded demo hold"},
        fields=[("combobox", "Share", "share"), ("combobox", "Reason Code", "reason"),
                ("textbox", "Notes", "notes")],
        clicks=["Continue", "Apply Hold"],
        extract=("Confirmation", "confirmation"),
        checkpoint_text="HOLD APPLIED",
        param_schema={
            "member_id": "member number",
            "share": "share id to place on hold, e.g. 100234-MMKT-13",
            "reason": "reason code: FRAUD, LEGAL, or DECEASED",
            "notes": "free-text notes",
        },
    ),
}


def record_one(name: str) -> None:
    spec = FLOWS[name]
    creds = credentials_for(spec["role"])
    rec_url = spec["url_template"].format(**spec["rec_params"])
    lit_to_param = {v: k for k, v in spec["rec_params"].items() if k != "member_id"}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        replay(load_signon_capability(), params=creds, page=page, run_id=f"rec_{name}_signon")
        page.goto(rec_url, timeout=20000)

        rec = Recorder(goal=spec["description"])
        rec.record_navigate(rec_url)
        for role, label, key in spec["fields"]:
            val = key.split("__literal__:", 1)[1] if str(key).startswith("__literal__:") \
                else spec["rec_params"][key]
            rec.record_type(role, label, val, page)
            execute_type(page, role, label, val)
        for label in spec["clicks"]:
            rec.record_click("button", label, page)
            execute_click(page, "button", label)
            page.wait_for_load_state()
        if spec.get("extract"):
            rec.record_extract("cell", spec["extract"][0] + ":", spec["extract"][1], page)
        for role, cellname, var in spec.get("extracts", []):
            rec.record_extract(role, cellname, var, page)
        final_body = page.content()
        browser.close()

    if spec["checkpoint_text"].lower() not in final_body.lower():
        print(f"  ! WARNING: checkpoint text {spec['checkpoint_text']!r} not found on the final "
              f"page — inspect the recorded flow for {name}")

    outputs = {}
    if spec.get("extract"):
        outputs[spec["extract"][1]] = ""
    for _, _, var in spec.get("extracts", []):
        outputs[var] = ""
    capability = compile_capability(
        capability_id=spec["capability_id"], version="1.0.0", run_id="scripted_recorder",
        target_url=spec["url_template"], risk_level=spec["risk_level"], recorder=rec,
        outputs=outputs,
        checkpoint=Checkpoint(type="text_match", locator=None, expected=spec["checkpoint_text"]),
        surface_type="legacy_web", description=spec["description"], app_name="meridian-core",
    )
    capability.steps[0].value = spec["url_template"]
    for step in capability.steps:
        if isinstance(step.value, str) and step.value in lit_to_param:
            step.value = {"param_ref": lit_to_param[step.value]}
    capability.requires_role = spec["requires_role"]
    capability.input_schema = {
        k: {"type": "string", "description": d} for k, d in spec["param_schema"].items()
    }

    path = save_capability(capability)
    print(f"wrote {path}  (risk={spec['risk_level']}, requires_role={spec['requires_role']})")
    for s in json.loads(Path(path).read_text())["steps"]:
        t = s.get("target") or {}
        print(f"  {s['step_id']:3} {s['action_type']:8} {t.get('strategy','-'):14} "
              f"{t.get('primary')} val={s.get('value')} as={s.get('extract_as')}")


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    names = list(FLOWS) if which == "all" else [which]
    for n in names:
        if n not in FLOWS:
            sys.exit(f"unknown flow {n!r}; one of {list(FLOWS)} or 'all'")
        print(f"\n=== recording {n} ===")
        record_one(n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
