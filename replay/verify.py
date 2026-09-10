"""
`verify_capability` - replay every declared branch of a capability against the live app and
check each lands where the artifact says it should. Turns the manual "re-sweep the whole
outcome matrix live after every change" discipline (REPORT §3) into a function with a stored
result: a capability only earns `lifecycle="verified"` by carrying a `VerificationRecord` whose
`all_passed` is True.

Scenarios come from `app_knowledge/<app>.yaml` (`capabilities.<id>.verify_scenarios`), so they
live with the rest of the curated per-app knowledge, not in code.
"""
from __future__ import annotations

import time
from collections.abc import Callable

import app_knowledge
from artifact.schema import Capability, Result, VerificationRecord
from replay.engine import replay


def _scenario_ok(scenario: dict, result: Result) -> tuple[bool, dict]:
    expect_status = scenario.get("expect", "success")
    expect_code = scenario.get("code")
    observed_code = result.business_outcome_code
    ok = result.status == expect_status and (expect_code is None or observed_code == expect_code)
    return ok, {
        "params": scenario.get("params", {}),
        "expected_status": expect_status,
        "expected_code": expect_code,
        "observed_status": result.status,
        "observed_code": observed_code,
        "ok": ok,
        "evidence_ref": result.evidence_ref,
    }


def verify_capability(
    capability: Capability,
    scenarios: list[dict] | None = None,
    *,
    app_name: str | None = None,
    _replay: Callable[..., Result] = replay,
) -> VerificationRecord:
    """
    Replay each scenario and build a VerificationRecord. `scenarios` defaults to the curated
    `verify_scenarios` for this capability. A scenario is
    `{params: {...}, expect: <status>, code: <business_outcome_code or None>}`.
    Risky capabilities are replayed with `confirm=True` so their full path (including the
    mutating step) is actually exercised.
    """
    app = app_name or capability.target.app_name
    if scenarios is None:
        scenarios = app_knowledge.load(app).capability_config(capability.capability_id).verify_scenarios
    scenarios = scenarios or []

    confirm = capability.risk_level == "risky"
    results, evidence = [], []
    for scenario in scenarios:
        result = _replay(capability, scenario.get("params", {}), confirm=confirm)
        _ok, row = _scenario_ok(scenario, result)
        results.append(row)
        if result.evidence_ref:
            evidence.append(result.evidence_ref)

    return VerificationRecord(
        ran_at=time.time(),
        all_passed=bool(results) and all(r["ok"] for r in results),
        scenarios=results,
        evidence_refs=evidence,
    )


def apply_verification(capability: Capability, record: VerificationRecord) -> Capability:
    """Attach the record and, if it passed and no rules are still unratified, promote
    draft -> verified. Returns a new Capability; never mutates the input."""
    lifecycle = capability.lifecycle
    if record.all_passed and not capability.unratified_rules() and lifecycle == "draft":
        lifecycle = "verified"
    return capability.model_copy(update={"verification": record, "lifecycle": lifecycle})
