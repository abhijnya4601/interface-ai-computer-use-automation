"""
Artifact compiler (Phase 4). On a successful discovery run, turns `Recorder.steps` into a
versioned, serializable `Capability` and writes it to `capabilities/<capability_id>.v<major>.json`.

One thing this module does beyond simply repackaging the recorder's steps: it declares
`expected_outcomes` on the steps where a business outcome or a known runtime condition can
occur, based on domain knowledge of the target app established while building it (see
`_KNOWN_OUTCOMES` below and DECISIONS.md) — not solely from what the single recorded discovery
run happened to observe. A single happy-path discovery run only ever sees the happy path; the
not-found and permission-denied branches (see app/templates/search.html's "No results." row and
member_detail.html's msg-denied branch) are real behaviors of the target app that a human
reviewer finalizing this artifact for production use would document from having explored the
app — exactly the same spirit as `LocatorTarget.reasoning` already asking a reviewer to explain
*why* a locator was chosen, not just recording it blindly.

Replay (Phase 5) evaluates these declared conditions against the live page deterministically —
it never guesses or calls an LLM to decide whether a business outcome occurred; it checks the
exact condition string the artifact itself declares.
"""
from __future__ import annotations

import json
from pathlib import Path

from artifact.schema import Capability, Checkpoint, ExpectedOutcome, Step, TargetSpec
from guardrails.policy import redact

CAPABILITIES_DIR = Path(__file__).parent.parent / "capabilities"

_KNOWN_OUTCOMES: dict[str, list[dict]] = {
    "lookup_member_balance": [
        {
            "match": {"action_type": "click", "role": "link", "name": "View"},
            "outcome": ExpectedOutcome(
                condition="page contains 'No results.'",
                classification="business_outcome",
                code="MEMBER_NOT_FOUND",
                handling="search returned no matching row for this member_id; there is no "
                "'View' link to click through — treat as not found rather than a broken locator",
            ),
        },
        {
            "match": {"action_type": "extract"},
            "outcome": ExpectedOutcome(
                condition="page contains 'Access denied. This account is restricted'",
                classification="business_outcome",
                code="PERMISSION_DENIED",
                handling="member exists but the account is locked; balance is not shown on this page",
            ),
        },
        {
            "match": {"action_type": "extract"},
            "outcome": ExpectedOutcome(
                condition="page contains 'No member record found'",
                classification="business_outcome",
                code="MEMBER_NOT_FOUND",
                handling="member record does not exist at this URL",
            ),
        },
    ],
    "open_subaccount": [
        {
            "match": {"action_type": "click", "role": "link", "name": "View"},
            "outcome": ExpectedOutcome(
                condition="page contains 'No results.'",
                classification="business_outcome",
                code="MEMBER_NOT_FOUND",
                handling="search returned no matching row for this member_id",
            ),
        },
        {
            # See DECISIONS.md D14: this used to be declared on the "Continue" click (s7),
            # assuming the flow would reach the sub-account form and get turned away there by
            # app.py's server-side status check. It doesn't — member_detail.html never renders
            # the "Open sub-account" link at all for a locked member (only the msg-denied
            # branch), so the wall is hit one click earlier, on this link, which is why this
            # rule shares its match with the MEMBER_NOT_FOUND rule above.
            "match": {"action_type": "click", "role": "link", "name": "Open sub-account"},
            "outcome": ExpectedOutcome(
                condition="page contains 'Access denied. This account is restricted'",
                classification="business_outcome",
                code="PERMISSION_DENIED",
                handling="member account is locked; there is no 'Open sub-account' link to "
                "click through on the member detail page",
            ),
        },
    ],
    "lookup_latest_transaction": [
        {
            "match": {"action_type": "click", "role": "link", "name": "View"},
            "outcome": ExpectedOutcome(
                condition="page contains 'No results.'",
                classification="business_outcome",
                code="MEMBER_NOT_FOUND",
                handling="search returned no matching row for this member_id",
            ),
        },
        {
            "match": {"action_type": "click", "role": "link", "name": "View Transactions"},
            "outcome": ExpectedOutcome(
                condition="page contains 'Access denied. This account is restricted'",
                classification="business_outcome",
                code="PERMISSION_DENIED",
                handling="member account is locked; transaction history is not shown",
            ),
        },
        {
            # D22: found live, not contrived — a member with zero transactions renders one row
            # with transactions.html's msg-empty text instead of a data row. The table_position
            # locator (row 0, column 0) still resolves to *a* cell at that position — it has no
            # way to know the row is a placeholder rather than data — so without this declared
            # outcome, replay would report status=success with "No transactions on file." as if
            # it were a real transaction date. Exactly the "business outcome silently treated as
            # success" failure mode the assignment calls out as the most common mistake here.
            "match": {"action_type": "extract"},
            "outcome": ExpectedOutcome(
                condition="page contains 'No transactions on file.'",
                classification="business_outcome",
                code="NO_TRANSACTIONS",
                handling="member has no transaction history; there is no real data to extract",
            ),
        },
    ],
}


def _step_matches(step: Step, match: dict) -> bool:
    if match.get("action_type") and step.action_type != match["action_type"]:
        return False
    if match.get("role"):
        if not step.target or step.target.primary.get("role") != match["role"]:
            return False
    if match.get("name"):
        if not step.target or step.target.primary.get("name") != match["name"]:
            return False
    return True


def _attach_expected_outcomes(capability_id: str, steps: list[Step]) -> list[Step]:
    rules = _KNOWN_OUTCOMES.get(capability_id, [])
    enriched = []
    for step in steps:
        outcomes = list(step.expected_outcomes)
        for rule in rules:
            if _step_matches(step, rule["match"]):
                outcomes.append(rule["outcome"])
        enriched.append(step.model_copy(update={"expected_outcomes": outcomes}))
    return enriched


def infer_input_schema(steps: list[Step]) -> dict:
    names = set()
    for step in steps:
        if isinstance(step.value, dict) and "param_ref" in step.value:
            names.add(step.value["param_ref"])
    return {
        name: {"type": "string", "description": f"the {name.replace('_', ' ')} to use for this run"}
        for name in sorted(names)
    }


def infer_output_schema(outputs: dict, steps: list[Step]) -> dict:
    """
    Declares only the output keys that have a recorded Step actually backing them (D22): a
    discovery run's `finish()` can report values the LLM read directly off the observation
    without ever calling `extract()` on them (see DECISIONS.md D21) — declaring those in
    `output_schema` anyway produces a schema-valid artifact whose promised outputs replay has no
    recorded way to reproduce. A key in `outputs` with no step whose `extract_as` matches it is
    dropped (with a printed warning, never silently) rather than promised and then missing.
    """
    backed_keys = {step.extract_as for step in steps if step.extract_as}
    schema = {}
    for key in outputs:
        if key in backed_keys:
            schema[key] = {"type": "string"}
        else:
            print(
                f"[compiler] WARNING: output {key!r} was reported by finish() but no recorded "
                f"step extracted it — dropping it from output_schema since replay has no way to "
                f"reproduce it. Steps actually extracted: {sorted(backed_keys) or 'none'}."
            )
    return schema


def compile_capability(
    capability_id: str,
    version: str,
    run_id: str,
    target_url: str,
    risk_level: str,
    recorder,
    outputs: dict,
    checkpoint: Checkpoint,
    surface_type: str = "legacy_web",
) -> Capability:
    steps = _attach_expected_outcomes(capability_id, recorder.steps)
    return Capability(
        capability_id=capability_id,
        version=version,
        created_from_run_id=run_id,
        target=TargetSpec(app_name="mock-core-banking", entry_point=target_url, surface_type=surface_type),
        risk_level=risk_level,
        input_schema=infer_input_schema(steps),
        output_schema=infer_output_schema(outputs, steps),
        checkpoint=checkpoint,
        steps=steps,
    )


def save_capability(capability: Capability, path: Path | None = None) -> Path:
    """
    Serializes and writes the capability, running `redact()` only over `steps` — never over
    `input_schema`/`output_schema`. Those two are pure type metadata (e.g. `{"type": "string"}`),
    never actual data, so there is nothing in them to redact; running redact() over the whole
    `model_dump()` corrupted a real artifact once (DECISIONS.md D13): a field legitimately named
    `sub_account_number` matched the `account_number` secret-key marker, and redact() replaced
    its entire schema-type dict with the string "***REDACTED***" — silently breaking the
    artifact's structural validity, not protecting any actual secret (there was never a real
    account number value anywhere near it, just a type declaration). `steps` is the one place a
    literal, potentially-sensitive value could actually appear (a `Step.value` the LLM typed),
    so that's the only part that goes through redact().
    """
    if path is None:
        major = capability.version.split(".")[0]
        path = CAPABILITIES_DIR / f"{capability.capability_id}.v{major}.json"
    path.parent.mkdir(exist_ok=True)
    dumped = capability.model_dump()
    dumped["steps"] = redact(dumped["steps"])
    path.write_text(json.dumps(dumped, indent=2, default=str))
    return path
