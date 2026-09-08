"""
Re-apply agent/compiler.py's compile-time enrichment (`_attach_expected_outcomes` and
`_attach_extract_contracts`) to every already-compiled capabilities/*.json and re-save it.

This is the established repair pattern for this build, made into a script instead of an ad-hoc
REPL session: when a rule in `app_knowledge/<app>.yaml` changes, the live artifacts need to
pick it up without a fresh (LLM-driven, non-deterministic) discovery run. Both enrichment
functions are idempotent by contract, so this is safe to run repeatedly and a no-op when
nothing changed. It reads each artifact's own `target.app_name` to pick the right YAML.

    python scripts/patch_capabilities.py           # patch all
    python scripts/patch_capabilities.py --check    # exit 1 if any file would change (for CI)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.compiler import _attach_expected_outcomes, _attach_extract_contracts, save_capability
from artifact.schema import Capability

CAPABILITIES_DIR = Path(__file__).parent.parent / "capabilities"


def _repatched(capability: Capability) -> Capability:
    app_name = capability.target.app_name
    steps = _attach_expected_outcomes(capability.capability_id, capability.steps, app_name=app_name)
    steps = _attach_extract_contracts(capability.capability_id, steps, app_name=app_name)
    return capability.model_copy(update={"steps": steps})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="don't write; exit 1 if any artifact would change")
    args = parser.parse_args()

    changed = []
    for path in sorted(CAPABILITIES_DIR.glob("*.json")):
        before = path.read_text()
        capability = Capability.model_validate_json(before)
        patched = _repatched(capability)

        if args.check:
            after = json.dumps(patched.model_dump(), indent=2, default=str)
            # save_capability also runs redact() over steps; for the check we only care whether
            # the enrichment itself is a no-op, so compare the pre-redaction dump.
            current = json.dumps(capability.model_dump(), indent=2, default=str)
            if after != current:
                changed.append(path.name)
            continue

        saved = save_capability(patched)
        if saved.read_text() != before:
            changed.append(saved.name)

    if args.check:
        if changed:
            print(f"would change: {', '.join(changed)}")
            return 1
        print("all capability artifacts are up to date")
        return 0

    print(f"patched {len(changed)} of {len(list(CAPABILITIES_DIR.glob('*.json')))} artifacts"
          + (f": {', '.join(changed)}" if changed else " (all already current)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
