import json

from agent.compiler import compile_capability, infer_input_schema, infer_output_schema, save_capability
from artifact.schema import Capability, Checkpoint, LocatorTarget, Step


class FakeRecorder:
    def __init__(self, steps):
        self.steps = steps
        self.tier_log = []


def _lt(role, name, strategy="role_name"):
    return LocatorTarget(strategy=strategy, primary={"role": role, "name": name}, fallbacks=[],
                          reasoning="test locator")


def _lookup_steps():
    return [
        Step(step_id="s1", action_type="navigate", value="http://localhost:5050/search"),
        Step(step_id="s2", action_type="type", target=_lt("textbox", "Search (ID / name)"),
             value={"param_ref": "member_id"}),
        Step(step_id="s3", action_type="click", target=_lt("button", "Go")),
        Step(step_id="s4", action_type="click", target=_lt("link", "View")),
        Step(step_id="s5", action_type="extract", target=_lt("rowheader", "Savings Balance"),
             extract_as="savings_balance"),
    ]


def test_infer_input_schema_finds_param_refs():
    schema = infer_input_schema(_lookup_steps())
    assert "member_id" in schema
    assert schema["member_id"]["type"] == "string"


def test_infer_output_schema_from_outputs_dict():
    schema = infer_output_schema({"savings_balance": "$1,842.30"})
    assert schema == {"savings_balance": {"type": "string"}}


def test_compile_attaches_business_outcomes_to_matching_steps():
    recorder = FakeRecorder(_lookup_steps())
    checkpoint = Checkpoint(type="element_present", locator={"role": "rowheader", "name": "Savings Balance"},
                             expected="present")
    cap = compile_capability(
        capability_id="lookup_member_balance", version="1.0.0", run_id="run_test",
        target_url="http://localhost:5050/search", risk_level="safe",
        recorder=recorder, outputs={"savings_balance": "$1,842.30"}, checkpoint=checkpoint,
    )

    view_step = next(s for s in cap.steps if s.step_id == "s4")
    codes = {o.code for o in view_step.expected_outcomes}
    assert "MEMBER_NOT_FOUND" in codes

    extract_step = next(s for s in cap.steps if s.step_id == "s5")
    codes = {o.code for o in extract_step.expected_outcomes}
    assert codes == {"PERMISSION_DENIED", "MEMBER_NOT_FOUND"}

    # untouched steps get no expected_outcomes injected
    navigate_step = next(s for s in cap.steps if s.step_id == "s1")
    assert navigate_step.expected_outcomes == []


def test_compile_produces_valid_capability_with_reasoning_on_every_locator():
    recorder = FakeRecorder(_lookup_steps())
    checkpoint = Checkpoint(type="element_present", locator={"role": "rowheader", "name": "Savings Balance"},
                             expected="present")
    cap = compile_capability(
        capability_id="lookup_member_balance", version="1.0.0", run_id="run_test",
        target_url="http://localhost:5050/search", risk_level="safe",
        recorder=recorder, outputs={"savings_balance": "$1,842.30"}, checkpoint=checkpoint,
    )
    for step in cap.steps:
        if step.target is not None:
            assert step.target.reasoning


def test_save_capability_round_trips_and_is_human_readable(tmp_path):
    recorder = FakeRecorder(_lookup_steps())
    checkpoint = Checkpoint(type="element_present", locator={"role": "rowheader", "name": "Savings Balance"},
                             expected="present")
    cap = compile_capability(
        capability_id="lookup_member_balance", version="1.0.0", run_id="run_test",
        target_url="http://localhost:5050/search", risk_level="safe",
        recorder=recorder, outputs={"savings_balance": "$1,842.30"}, checkpoint=checkpoint,
    )
    path = save_capability(cap, path=tmp_path / "lookup_member_balance.v1.json")
    assert path.exists()

    raw = path.read_text()
    assert "step_id" in raw and "reasoning" in raw  # human-readable, not a blob

    reloaded = Capability.model_validate_json(raw)
    assert reloaded.capability_id == "lookup_member_balance"
    assert len(reloaded.steps) == 5
