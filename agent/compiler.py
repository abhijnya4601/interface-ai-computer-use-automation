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
            "match": {"action_type": "click", "role": "button", "name": "Continue"},
            "outcome": ExpectedOutcome(
                condition="page contains 'Access denied. Member'",
                classification="business_outcome",
                code="PERMISSION_DENIED",
                handling="member account is locked; sub-account creation is not permitted",
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


def infer_output_schema(outputs: dict) -> dict:
    return {key: {"type": "string"} for key in outputs}


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
        output_schema=infer_output_schema(outputs),
        checkpoint=checkpoint,
        steps=steps,
    )


def save_capability(capability: Capability, path: Path | None = None) -> Path:
    if path is None:
        major = capability.version.split(".")[0]
        path = CAPABILITIES_DIR / f"{capability.capability_id}.v{major}.json"
    path.parent.mkdir(exist_ok=True)
    redacted = redact(capability.model_dump())
    path.write_text(json.dumps(redacted, indent=2, default=str))
    return path
