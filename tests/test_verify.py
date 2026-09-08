from artifact.schema import Capability, Checkpoint, Result, Step, TargetSpec
from replay.verify import apply_verification, verify_capability


def _cap(risk="safe", proposed=False):
    steps = [Step(step_id="s1", action_type="navigate", value="http://x/")]
    if proposed:
        from artifact.schema import ExpectedOutcome
        steps[0] = steps[0].model_copy(update={"expected_outcomes": [
            ExpectedOutcome(condition="page contains 'x'", classification="business_outcome",
                            code="X", provenance="proposed"),
        ]})
    return Capability(
        capability_id="c", version="1.0.0", created_from_run_id="r",
        target=TargetSpec(app_name="mock-core-banking", entry_point="http://x/"),
        risk_level=risk, input_schema={}, output_schema={},
        checkpoint=Checkpoint(type="url_match", expected="x"), steps=steps,
    )


def _stub(results_by_member):
    def _run(capability, params, *, confirm=False, headless=True, run_id=None):
        return results_by_member[params["member_id"]]
    return _run


def test_all_scenarios_matching_gives_all_passed_and_promotes_to_verified():
    scenarios = [
        {"params": {"member_id": "1"}, "expect": "success"},
        {"params": {"member_id": "2"}, "expect": "business_outcome", "code": "MEMBER_NOT_FOUND"},
    ]
    stub = _stub({
        "1": Result(status="success"),
        "2": Result(status="business_outcome", business_outcome_code="MEMBER_NOT_FOUND"),
    })
    rec = verify_capability(_cap(), scenarios, _replay=stub)
    assert rec.all_passed is True
    assert all(r["ok"] for r in rec.scenarios)

    updated = apply_verification(_cap(), rec)
    assert updated.lifecycle == "verified"
    assert updated.verification.all_passed


def test_a_wrong_code_fails_the_scenario_and_blocks_verified():
    scenarios = [{"params": {"member_id": "2"}, "expect": "business_outcome", "code": "MEMBER_NOT_FOUND"}]
    stub = _stub({"2": Result(status="business_outcome", business_outcome_code="PERMISSION_DENIED")})
    rec = verify_capability(_cap(), scenarios, _replay=stub)
    assert rec.all_passed is False
    assert apply_verification(_cap(), rec).lifecycle == "draft"


def test_unratified_proposed_rules_block_promotion_even_when_scenarios_pass():
    scenarios = [{"params": {"member_id": "1"}, "expect": "success"}]
    stub = _stub({"1": Result(status="success")})
    cap = _cap(proposed=True)
    rec = verify_capability(cap, scenarios, _replay=stub)
    assert rec.all_passed is True
    assert apply_verification(cap, rec).lifecycle == "draft"  # still draft: a rule needs review


def test_risky_capability_is_replayed_with_confirm_true():
    seen = {}

    def _run(capability, params, *, confirm=False, headless=True, run_id=None):
        seen["confirm"] = confirm
        return Result(status="business_outcome", business_outcome_code="MEMBER_NOT_FOUND")

    verify_capability(
        _cap(risk="risky"),
        [{"params": {"member_id": "2"}, "expect": "business_outcome", "code": "MEMBER_NOT_FOUND"}],
        _replay=_run,
    )
    assert seen["confirm"] is True


def test_no_scenarios_is_not_a_pass():
    rec = verify_capability(_cap(), [], _replay=_stub({}))
    assert rec.all_passed is False
