"""
Artifact compiler (Phase 4). On a successful discovery run, turns `Recorder.steps` into a
versioned, serializable `Capability` and writes it to `capabilities/<capability_id>.v<major>.json`.

Beyond repackaging the recorder's steps, this module attaches two kinds of domain knowledge the
single happy-path discovery run doesn't observe on its own - the not-found / permission-denied
branches (`expected_outcomes`) and what a real extracted value looks like (`extract_contract`).

That knowledge comes from two places, and every rule on the artifact is tagged with which:

  - **curated** - `app_knowledge/<app_name>.yaml`, loaded by `target.app_name`. A human has
    signed off. This used to be Python dicts keyed by capability_id right here in this file,
    which meant onboarding a new target app required a code change; now it's data, per app.
  - **proposed** - the discovery agent's own `note_branch` / `note_data_shape` tool calls from
    what it explored this run (`Recorder.proposed_outcomes` / `.proposed_contracts`). Replay
    uses these (they're a real signal), but the capability's `lifecycle` stays `"draft"` and
    `Capability.unratified_rules()` is non-empty until a reviewer promotes them into the YAML
    with `scripts/review_capability.py`.

Replay (Phase 5) evaluates these declared conditions against the live page deterministically -
it never guesses or calls an LLM to decide whether a business outcome occurred.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import app_knowledge
from artifact.schema import (
    Capability,
    Checkpoint,
    ExpectedOutcome,
    ExtractContract,
    Step,
    TargetSpec,
)
from common.logging import get_logger
from guardrails.policy import redact

# Overridable so a hosted deployment can point it at a persistent volume (see DEPLOY.md);
# defaults to the repo's own capabilities/ for local use and CI.
CAPABILITIES_DIR = Path(os.environ.get("CAPABILITIES_DIR") or (Path(__file__).parent.parent / "capabilities"))
DEFAULT_APP_NAME = "mock-core-banking"
_log = get_logger("compiler")


def _step_matches(step: Step, match: dict) -> bool:
    if match.get("action_type") and step.action_type != match["action_type"]:
        return False
    if match.get("role"):
        if not step.target or step.target.primary.get("role") != match["role"]:
            return False
    if match.get("name"):
        if not step.target or step.target.primary.get("name") != match["name"]:
            return False
    return not (match.get("step_id") and step.step_id != match["step_id"])


def _proposed_outcome_rules(proposed_outcomes: list[dict] | None) -> list[dict]:
    """Turn Recorder.proposed_outcomes entries into the same {match, outcome} shape the curated
    rules use, tagged `provenance='proposed'`."""
    rules = []
    for p in proposed_outcomes or []:
        match = {k: p[k] for k in ("action_type", "role", "name", "step_id") if p.get(k)}
        rules.append({
            "match": match,
            "outcome": ExpectedOutcome(
                condition=p["condition"],
                classification=p.get("classification", "business_outcome"),
                code=p.get("code"),
                handling=p.get("handling"),
                provenance="proposed",
            ),
        })
    return rules


def _attach_expected_outcomes(
    capability_id: str,
    steps: list[Step],
    *,
    app_name: str = DEFAULT_APP_NAME,
    proposed_outcomes: list[dict] | None = None,
) -> list[Step]:
    """
    Attach curated (YAML) + proposed (this run's) expected-outcome rules to the steps they
    match. Idempotent and dedup'd on `(condition, code)` - running twice, or re-running against
    an already-compiled artifact, produces the same result. Curated wins a tie with proposed
    (so promoting a proposed rule into the YAML and recompiling flips its provenance without a
    duplicate).
    """
    knowledge = app_knowledge.load(app_name)
    curated = knowledge.outcome_rules(capability_id)
    proposed = _proposed_outcome_rules(proposed_outcomes)

    enriched = []
    for step in steps:
        outcomes = list(step.expected_outcomes)
        seen = {(o.condition, o.code): i for i, o in enumerate(outcomes)}
        for rule in (*curated, *proposed):
            if not _step_matches(step, rule["match"]):
                continue
            key = (rule["outcome"].condition, rule["outcome"].code)
            if key in seen:
                existing = outcomes[seen[key]]
                # a curated rule upgrades a matching proposed/duplicate one in place
                if rule["outcome"].provenance == "curated" and existing.provenance != "curated":
                    outcomes[seen[key]] = rule["outcome"]
                continue
            seen[key] = len(outcomes)
            outcomes.append(rule["outcome"])
        enriched.append(step.model_copy(update={"expected_outcomes": outcomes}))
    return enriched


def _attach_extract_contracts(
    capability_id: str,
    steps: list[Step],
    *,
    app_name: str = DEFAULT_APP_NAME,
    proposed_contracts: list[dict] | None = None,
) -> list[Step]:
    """
    Attach a declared ExtractContract to each `extract` step whose `extract_as` has one, curated
    (YAML) preferred over proposed (this run's). Idempotent: a step that already carries a
    contract keeps it unless a curated contract can replace a non-curated one.
    """
    knowledge = app_knowledge.load(app_name)
    contracts: dict[str, ExtractContract] = dict(knowledge.extract_contracts(capability_id))
    for p in proposed_contracts or []:
        key = p.get("extract_as")
        if key and key not in contracts:  # curated already wins by being inserted first
            contracts[key] = ExtractContract(
                pattern=p.get("pattern"),
                nonempty=p.get("nonempty", True),
                placeholders=list(p.get("placeholders", [])),
                reason=p.get("reason", ""),
                provenance="proposed",
            )
    if not contracts:
        return steps

    enriched = []
    for step in steps:
        want = contracts.get(step.extract_as) if step.action_type == "extract" else None
        have = step.extract_contract
        if want is not None and (
            have is None or (want.provenance == "curated" and have.provenance != "curated")
        ):
            enriched.append(step.model_copy(update={"extract_contract": want}))
        else:
            enriched.append(step)
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
    Declares only the output keys that have a recorded Step actually backing them: a
    discovery run's `finish()` can report values the LLM read directly off the observation
    without ever calling `extract()` on them - declaring those in
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
            _log.warning(
                "unbacked_output_dropped", output=key,
                extracted=sorted(backed_keys) or None,
                detail="reported by finish() but no recorded step extracted it; replay could "
                       "not reproduce it",
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
    description: str = "",
    app_name: str = DEFAULT_APP_NAME,
) -> Capability:
    proposed_outcomes = getattr(recorder, "proposed_outcomes", None)
    proposed_contracts = getattr(recorder, "proposed_contracts", None)

    steps = _attach_expected_outcomes(
        capability_id, recorder.steps, app_name=app_name, proposed_outcomes=proposed_outcomes
    )
    steps = _attach_extract_contracts(
        capability_id, steps, app_name=app_name, proposed_contracts=proposed_contracts
    )
    capability = Capability(
        capability_id=capability_id,
        version=version,
        created_from_run_id=run_id,
        description=description,
        target=TargetSpec(app_name=app_name, entry_point=target_url, surface_type=surface_type),
        risk_level=risk_level,
        input_schema=infer_input_schema(steps),
        output_schema=infer_output_schema(outputs, steps),
        checkpoint=checkpoint,
        steps=steps,
    )
    # A fresh compile is always a draft; it only becomes `verified` after replay.verify runs
    # every declared branch clean (scripts/verify_capability.py), and only `published` when a
    # human clears it - see scripts/review_capability.py.
    capability.lifecycle = "draft"
    return capability


def save_capability(capability: Capability, path: Path | None = None) -> Path:
    """
    Serializes and writes the capability, running `redact()` only over `steps` - never over
    `input_schema`/`output_schema`. Those two are pure type metadata (e.g. `{"type": "string"}`),
    never actual data, so there is nothing in them to redact; running redact() over the whole
    `model_dump()` corrupted a real artifact once: a field legitimately named
    `sub_account_number` matched the `account_number` secret-key marker, and redact() replaced
    its entire schema-type dict with the string "***REDACTED***" - silently breaking the
    artifact's structural validity, not protecting any actual secret. `steps` is the one place a
    literal, potentially-sensitive value could actually appear (a `Step.value` the LLM typed),
    so that's the only part that goes through redact().
    """
    if path is None:
        major = capability.version.split(".")[0]
        path = CAPABILITIES_DIR / f"{capability.capability_id}.v{major}.json"
    path.parent.mkdir(exist_ok=True)
    dumped = capability.model_dump()
    dumped["steps"] = redact(dumped["steps"])
    path.write_text(json.dumps(dumped, indent=2, default=str) + "\n")
    return path
