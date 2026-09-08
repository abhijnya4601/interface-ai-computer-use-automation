"""
Review the agent-proposed rules on a compiled capability and promote the good ones into the
curated knowledge file, so a new target app is onboarded by review, not by editing Python.

    python scripts/review_capability.py capabilities/foo.v1.json              # list proposals
    python scripts/review_capability.py capabilities/foo.v1.json --promote-all  # accept all
    python scripts/review_capability.py capabilities/foo.v1.json --publish       # verified -> published

`--promote-all` appends every `provenance="proposed"` rule to
`app_knowledge/<app>.yaml`, then recompiles the artifact so those rules come back as
`provenance="curated"` (no duplication — the compiler dedups on condition/code). Review the
YAML diff before committing it.
"""
import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.compiler import _attach_expected_outcomes, _attach_extract_contracts, save_capability
from artifact.schema import Capability

KNOWLEDGE_DIR = Path(__file__).parent.parent / "app_knowledge"


def _proposed(capability: Capability):
    outcomes, contracts = [], []
    for step in capability.steps:
        for o in step.expected_outcomes:
            if o.provenance == "proposed":
                match = {"action_type": step.action_type}
                if step.target:
                    match["role"] = step.target.primary.get("role")
                    match["name"] = step.target.primary.get("name")
                outcomes.append({
                    "match": {k: v for k, v in match.items() if v},
                    "condition": o.condition, "classification": o.classification,
                    "code": o.code, "handling": o.handling,
                })
        c = step.extract_contract
        if c and c.provenance == "proposed":
            contracts.append({step.extract_as: {
                "pattern": c.pattern, "placeholders": c.placeholders, "reason": c.reason,
            }})
    return outcomes, contracts


def _promote(capability: Capability, path: Path) -> None:
    app = capability.target.app_name
    yaml_path = KNOWLEDGE_DIR / f"{app}.yaml"
    data = yaml.safe_load(yaml_path.read_text()) if yaml_path.exists() else {"app_name": app}
    data.setdefault("expected_outcomes", {}).setdefault(capability.capability_id, [])
    data.setdefault("extract_contracts", {}).setdefault(capability.capability_id, {})

    outcomes, contracts = _proposed(capability)
    for rule in outcomes:
        data["expected_outcomes"][capability.capability_id].append(rule)
    for c in contracts:
        data["extract_contracts"][capability.capability_id].update(c)
    yaml_path.write_text(yaml.safe_dump(data, sort_keys=False, width=100))
    print(f"appended {len(outcomes)} outcome rule(s) and {len(contracts)} contract(s) to {yaml_path}")

    # recompile so the promoted rules come back as curated (the compiler dedups the overlap)
    steps = _attach_expected_outcomes(capability.capability_id, capability.steps, app_name=app)
    steps = _attach_extract_contracts(capability.capability_id, steps, app_name=app)
    recompiled = capability.model_copy(update={"steps": steps})
    save_capability(recompiled, path=path)
    print(f"recompiled {path} — remaining unratified: {recompiled.unratified_rules() or 'none'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact")
    parser.add_argument("--promote-all", action="store_true")
    parser.add_argument("--publish", action="store_true",
                        help="flip lifecycle verified -> published (requires a passing VerificationRecord)")
    args = parser.parse_args()

    path = Path(args.artifact)
    capability = Capability.model_validate_json(path.read_text())
    unratified = capability.unratified_rules()

    print(f"{capability.capability_id}  lifecycle={capability.lifecycle}  "
          f"verified={'yes' if capability.verification and capability.verification.all_passed else 'no'}")
    if unratified:
        print(f"\n{len(unratified)} unratified proposed rule(s):")
        for u in unratified:
            print(f"  - {u}")
    else:
        print("\nno unratified rules")

    if args.promote_all and unratified:
        _promote(capability, path)
        return 0

    if args.publish:
        if not (capability.verification and capability.verification.all_passed):
            print("\nrefusing to publish: no passing VerificationRecord "
                  "(run scripts/verify_capability.py --write first)")
            return 1
        if capability.unratified_rules():
            print("\nrefusing to publish: unratified proposed rules remain")
            return 1
        save_capability(capability.model_copy(update={"lifecycle": "published"}), path=path)
        print(f"\n{capability.capability_id}: verified -> published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
