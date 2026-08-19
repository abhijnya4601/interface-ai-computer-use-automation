import pytest
from pydantic import ValidationError

from artifact.schema import (
    Capability,
    Checkpoint,
    ExpectedOutcome,
    LocatorTarget,
    Result,
    Step,
    TargetSpec,
    WaitPolicy,
)


def _sample_capability() -> Capability:
    return Capability(
        capability_id="lookup_member_balance",
        version="1.0.0",
        created_from_run_id="run_test_001",
        target=TargetSpec(
            app_name="mock-core-banking", entry_point="http://localhost:5050/search"
        ),
        risk_level="safe",
        input_schema={"member_id": {"type": "string"}},
        output_schema={"savings_balance": {"type": "string"}},
        checkpoint=Checkpoint(
            type="element_present", locator={"role": "cell"}, expected="balance visible"
        ),
        steps=[
            Step(
                step_id="s1",
                action_type="navigate",
                value="http://localhost:5050/search",
            ),
            Step(
                step_id="s2",
                action_type="type",
                target=LocatorTarget(
                    strategy="role_name",
                    primary={"role": "textbox", "name": "Search (ID / name)"},
                    fallbacks=[],
                    reasoning="the search box has a stable label via <label for=...>",
                ),
                value={"param_ref": "member_id"},
            ),
        ],
    )


def test_capability_round_trips_through_json():
    cap = _sample_capability()
    raw = cap.model_dump_json(indent=2)
    reloaded = Capability.model_validate_json(raw)
    assert reloaded == cap


def test_step_value_accepts_literal_string():
    step = Step(step_id="s1", action_type="type", value="christmas_club")
    assert step.value == "christmas_club"


def test_step_value_accepts_param_ref_dict():
    step = Step(step_id="s2", action_type="type", value={"param_ref": "member_id"})
    assert step.value == {"param_ref": "member_id"}


def test_locator_target_requires_reasoning():
    with pytest.raises(ValidationError):
        LocatorTarget(strategy="role_name", primary={"role": "button", "name": "Go"})


def test_expected_outcome_rejects_unknown_classification():
    with pytest.raises(ValidationError):
        ExpectedOutcome(condition="x", classification="not_a_real_classification")


def test_expected_outcome_business_outcome_carries_code():
    outcome = ExpectedOutcome(
        condition="page contains 'No member record found'",
        classification="business_outcome",
        code="MEMBER_NOT_FOUND",
    )
    assert outcome.code == "MEMBER_NOT_FOUND"


def test_capability_rejects_invalid_risk_level():
    bad = {**_sample_capability().model_dump(), "risk_level": "extremely_dangerous"}
    with pytest.raises(ValidationError):
        Capability.model_validate(bad)


def test_wait_policy_defaults():
    policy = WaitPolicy()
    assert policy.timeout_ms == 5000
    assert policy.retry_count == 2
    assert policy.retry_on == ["transient_load"]


def test_result_status_enum_rejects_unknown_value():
    with pytest.raises(ValidationError):
        Result(status="something_else")


def test_result_defaults_are_empty():
    result = Result(status="success", outputs={"savings_balance": "$1,842.30"})
    assert result.business_outcome_code is None
    assert result.failure_detail is None
