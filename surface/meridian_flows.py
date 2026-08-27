"""
MERIDIAN CORE flow specs — the small amount of per-capability knowledge that turns a raw
recording (whether from a live LLM discovery run or the scripted recorder) into a
parameterised, reviewable capability: the entry-point URL as a `{member_id}` template, which
recorded literal maps to which typed param, the typed input schema, the risk level, the operator
role the session must use, and the success-checkpoint text.

Used by:
  - scripts/discover_all_meridian.py / scripts/run_discovery.py --generalize  (LLM discovery)
  - scripts/record_meridian_flow.py                                            (scripted recorder)

`generalize(capability, flow_key)` applies a spec to a freshly-compiled Capability in place and
returns it.
"""
from __future__ import annotations

from artifact.schema import Capability, Checkpoint

BASE = "https://web-sample.interface-hiring.com"

FLOWS: dict[str, dict] = {
    "check_member_balance": dict(
        capability_id="meridian_check_member_balance",
        url_template=BASE + "/members",
        role="teller",
        risk_level="safe",
        requires_role=None,
        description="Look up a member and read the first share's id, type, balance and status.",
        rec_params={"member_id": "101555"},
        param_schema={"member_id": "member number to look up"},
        checkpoint_text=None,  # keep run_discovery's default (url_match 'members')
    ),
    "inquiry_by_name": dict(
        capability_id="meridian_member_inquiry_by_name",
        url_template=BASE + "/members",
        role="teller",
        risk_level="safe",
        requires_role=None,
        description="Find a member by last name and return the first match's number and name.",
        rec_params={"last_name": "Lovelace"},
        param_schema={"last_name": "member's last name to search for"},
        checkpoint_text="Select",
    ),
    "funds_transfer": dict(
        capability_id="meridian_funds_transfer",
        url_template=BASE + "/members/{member_id}/transfer",
        role="teller",
        risk_level="risky",
        requires_role=None,
        description="Transfer funds from one of a member's shares to another and post it.",
        rec_params={"member_id": "100234", "from_share": "100234-MMKT-10",
                    "to_share": "100234-S0001-11", "amount": "1.00", "memo": "recorded transfer"},
        param_schema={
            "member_id": "member number",
            "from_share": "source share id (must be OPEN, not HOLD)",
            "to_share": "destination share id",
            "amount": "amount to transfer, e.g. 1.00",
            "memo": "free-text memo",
        },
        checkpoint_text="TRANSFER POSTED",
    ),
    "open_share": dict(
        capability_id="meridian_open_share",
        url_template=BASE + "/members/{member_id}/open-share",
        role="teller",
        risk_level="risky",
        requires_role=None,
        description="Open a new share of a given type for a member with an initial deposit.",
        rec_params={"member_id": "100234", "share_type": "MMKT", "deposit": "5.00"},
        param_schema={
            "member_id": "member number to open the share for",
            "share_type": "share type code: S0001, S0070, MMKT, or CERT",
            "deposit": "initial deposit amount, e.g. 5.00",
        },
        checkpoint_text="SHARE OPENED",
    ),
    "update_info": dict(
        capability_id="meridian_update_info",
        url_template=BASE + "/members/{member_id}/update",
        role="teller",
        risk_level="risky",
        requires_role=None,
        description="Update a member's e-mail, phone and mailing address.",
        rec_params={"member_id": "100234", "email": "ada.lovelace@example.com",
                    "phone": "555-0142", "address": "12 Analytical Engine Rd"},
        param_schema={
            "member_id": "member number to update",
            "email": "new e-mail address",
            "phone": "new phone number",
            "address": "new mailing address",
        },
        checkpoint_text="INFORMATION UPDATED",
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
        param_schema={
            "member_id": "member number",
            "share": "share id to place on hold, e.g. 100234-MMKT-13",
            "reason": "reason code: FRAUD, LEGAL, or DECEASED",
            "notes": "free-text notes",
        },
        checkpoint_text="HOLD APPLIED",
    ),
    "signon": dict(
        capability_id="meridian_signon",
        url_template=BASE + "/signon",
        role=None,
        risk_level="safe",
        requires_role=None,
        description="Sign on to MERIDIAN CORE with an operator ID, password and branch.",
        rec_params={"operator": "teller1", "password": "password", "branch": "MAIN-001"},
        param_schema={
            "operator": "MERIDIAN CORE operator ID to sign on with",
            "password": "operator password (supplied from the environment, never stored)",
            "branch": "branch code, e.g. MAIN-001",
        },
        checkpoint_text=None,  # run_discovery default (url_match 'menu')
        member_id_in_url=False,
    ),
}


def generalize(capability: Capability, flow_key: str) -> Capability:
    """
    Apply the flow spec to a freshly-compiled Capability, in place: rewrite the entry-point URL
    to the `{member_id}` template (if the flow has one), swap every recorded literal that equals
    a `rec_params` value to `{"param_ref": <name>}`, set the typed input schema, risk level,
    required role, and (if given) a text-match checkpoint.
    """
    spec = FLOWS[flow_key]
    lit_to_param = {
        str(v): k for k, v in spec["rec_params"].items()
        if not (k == "member_id" and spec.get("member_id_in_url", True))
    }

    capability.steps[0].value = spec["url_template"]
    for step in capability.steps:
        if isinstance(step.value, str) and step.value in lit_to_param:
            step.value = {"param_ref": lit_to_param[step.value]}
    capability.risk_level = spec["risk_level"]
    capability.requires_role = spec["requires_role"]
    capability.input_schema = {
        k: {"type": "string", "description": d} for k, d in spec["param_schema"].items()
    }
    if spec.get("checkpoint_text"):
        capability.checkpoint = Checkpoint(
            type="text_match", locator=None, expected=spec["checkpoint_text"])
    return capability
