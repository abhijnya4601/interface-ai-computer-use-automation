from agent.compiler import (
    _attach_expected_outcomes,
    compile_capability,
    infer_input_schema,
    infer_output_schema,
    save_capability,
)
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
    steps = [Step(step_id="s1", action_type="extract", extract_as="savings_balance")]
    schema = infer_output_schema({"savings_balance": "$1,842.30"}, steps)
    assert schema == {"savings_balance": {"type": "string"}}


def test_infer_output_schema_drops_unbacked_keys():
    """An output the LLM reported via finish() but never actually extract()-ed must not be
    declared in output_schema -- replay has no recorded step that could reproduce it."""
    steps = [Step(step_id="s1", action_type="extract", extract_as="most_recent_date")]
    schema = infer_output_schema(
        {"most_recent_date": "2026-08-15", "member_name": "Dana Whitfield"}, steps
    )
    assert schema == {"most_recent_date": {"type": "string"}}
    assert "member_name" not in schema


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


def test_open_subaccount_permission_denied_attaches_to_the_open_subaccount_link_not_continue():
    """Regression test for a real bug: member_detail.html never renders the
    "Open sub-account" link at all for a locked member (only the msg-denied branch) -- so a
    locked member never even reaches the "Continue" step. The PERMISSION_DENIED outcome has to
    be declared on the "Open sub-account" link click (where the flow actually dead-ends for a
    locked member), not on "Continue" (which a locked member's flow never reaches)."""
    steps = [
        Step(step_id="s4", action_type="click", target=_lt("link", "View")),
        Step(step_id="s5", action_type="click", target=_lt("link", "Open sub-account")),
        Step(step_id="s7", action_type="click", target=_lt("button", "Continue")),
    ]
    recorder = FakeRecorder(steps)
    checkpoint = Checkpoint(type="text_match", expected="created for")
    cap = compile_capability(
        capability_id="open_subaccount", version="1.0.0", run_id="run_test",
        target_url="http://localhost:5050/search", risk_level="risky",
        recorder=recorder, outputs={}, checkpoint=checkpoint,
    )

    open_link_step = next(s for s in cap.steps if s.step_id == "s5")
    continue_step = next(s for s in cap.steps if s.step_id == "s7")
    assert {o.code for o in open_link_step.expected_outcomes} == {"PERMISSION_DENIED"}
    assert continue_step.expected_outcomes == []


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


def test_save_capability_does_not_corrupt_output_schema_with_a_secret_like_field_name(tmp_path):
    """Regression test for a real bug: a field legitimately named
    'sub_account_number' collided with the 'account_number' redaction marker, and applying
    redact() to the whole capability dump replaced its {"type": "string"} schema descriptor
    with the string "***REDACTED***", corrupting the artifact's structure."""
    recorder = FakeRecorder([
        Step(step_id="s1", action_type="navigate", value="http://x/search"),
        Step(step_id="s2", action_type="extract", target=_lt("cell", "2"), extract_as="sub_account_number"),
    ])
    checkpoint = Checkpoint(type="text_match", expected="created for")
    cap = compile_capability(
        capability_id="open_subaccount", version="1.0.0", run_id="run_test",
        target_url="http://x/search", risk_level="risky",
        recorder=recorder, outputs={"sub_account_number": "2"}, checkpoint=checkpoint,
    )
    path = save_capability(cap, path=tmp_path / "open_subaccount.v1.json")

    reloaded = Capability.model_validate_json(path.read_text())
    assert reloaded.output_schema["sub_account_number"] == {"type": "string"}


def test_save_capability_still_redacts_secret_like_values_inside_steps(tmp_path):
    # redact() matches by dict KEY, not by inspecting string contents — so the value has to be
    # shaped as a dict with a secret-like key (e.g. a hypothetical {"param_ref": ...}-style
    # value carrying a literal under a "password" key) to actually exercise the redaction path.
    recorder = FakeRecorder([
        Step(step_id="s1", action_type="type", target=_lt("textbox", "Password"),
             value={"password": "hunter2"}),
    ])
    checkpoint = Checkpoint(type="url_match", expected="/done")
    cap = compile_capability(
        capability_id="test_cap", version="1.0.0", run_id="run_test",
        target_url="http://x/", risk_level="safe",
        recorder=recorder, outputs={}, checkpoint=checkpoint,
    )
    path = save_capability(cap, path=tmp_path / "test_cap.v1.json")
    raw = path.read_text()
    assert "hunter2" not in raw
    assert "***REDACTED***" in raw


def test_lookup_latest_transaction_declares_no_transactions_business_outcome():
    """An empty transaction history renders a placeholder row that a position-based
    locator would otherwise silently treat as real data."""
    steps = [
        Step(step_id="s6", action_type="extract",
             target=_lt("cell", "2026-08-15", strategy="table_position"),
             extract_as="most_recent_date"),
    ]
    recorder = FakeRecorder(steps)
    checkpoint = Checkpoint(type="url_match", expected="transactions")
    cap = compile_capability(
        capability_id="lookup_latest_transaction", version="1.0.0", run_id="run_test",
        target_url="http://localhost:5050/search", risk_level="safe",
        recorder=recorder, outputs={"most_recent_date": "2026-08-15"}, checkpoint=checkpoint,
    )
    s6 = next(s for s in cap.steps if s.step_id == "s6")
    codes = {o.code for o in s6.expected_outcomes}
    assert "NO_TRANSACTIONS" in codes


def test_dispute_transaction_declares_permission_denied_on_view_transactions_link():
    """Found live -- dispute_transaction and update_member_address were compiled after the
    "locked member never renders the link" pattern was established for open_subaccount, but
    never got their own _KNOWN_OUTCOMES entries, since they're outside the two capabilities the
    assignment requires. Replaying either against a locked member_id was reporting hard_failure
    instead of the real, expected PERMISSION_DENIED business outcome -- verified live before and
    after this fix."""
    steps = [
        Step(step_id="s4", action_type="click", target=_lt("link", "View")),
        Step(step_id="s5", action_type="click", target=_lt("link", "View Transactions")),
        Step(step_id="s6", action_type="click", target=_lt("link", "Dispute", "structural")),
    ]
    recorder = FakeRecorder(steps)
    checkpoint = Checkpoint(type="text_match", expected="Dispute submitted for")
    cap = compile_capability(
        capability_id="dispute_transaction", version="1.0.0", run_id="run_test",
        target_url="http://localhost:5050/search", risk_level="risky",
        recorder=recorder, outputs={}, checkpoint=checkpoint,
    )
    view_step = next(s for s in cap.steps if s.step_id == "s4")
    view_txns_step = next(s for s in cap.steps if s.step_id == "s5")
    assert {o.code for o in view_step.expected_outcomes} == {"MEMBER_NOT_FOUND"}
    assert {o.code for o in view_txns_step.expected_outcomes} == {"PERMISSION_DENIED"}


def test_update_member_address_declares_permission_denied_on_its_own_link():
    """Same finding and fix, for the other capability that shares the gap."""
    steps = [
        Step(step_id="s4", action_type="click", target=_lt("link", "View")),
        Step(step_id="s5", action_type="click", target=_lt("link", "Update Mailing Address")),
    ]
    recorder = FakeRecorder(steps)
    checkpoint = Checkpoint(type="url_match", expected="update-address")
    cap = compile_capability(
        capability_id="update_member_address", version="1.0.0", run_id="run_test",
        target_url="http://localhost:5050/search", risk_level="risky",
        recorder=recorder, outputs={}, checkpoint=checkpoint,
    )
    update_link_step = next(s for s in cap.steps if s.step_id == "s5")
    assert {o.code for o in update_link_step.expected_outcomes} == {"PERMISSION_DENIED"}


def test_attach_expected_outcomes_is_idempotent():
    """Found live -- a full outcome-matrix sweep across all 5 real capabilities caught
    lookup_latest_transaction silently missing MEMBER_NOT_FOUND/PERMISSION_DENIED on its
    compiled artifact, even though agent/compiler.py's _KNOWN_OUTCOMES has always declared
    them -- stale-artifact drift. Patching it the established way (re-run
    _attach_expected_outcomes against the loaded artifact's steps, re-save) doubled
    the NO_TRANSACTIONS outcome already correctly present on the extract step, because
    `outcomes = list(step.expected_outcomes)` started from what was already there and every
    matching rule got appended unconditionally, with no check for "is this exact rule already
    present." Running this compile-time function against an already-compiled artifact is exactly
    the established repair pattern for this whole build -- it has to be safe to run twice."""
    steps = [
        Step(step_id="s6", action_type="extract",
             target=_lt("cell", "2026-08-15", strategy="table_position"),
             extract_as="most_recent_date"),
    ]
    once = _attach_expected_outcomes("lookup_latest_transaction", steps)
    twice = _attach_expected_outcomes("lookup_latest_transaction", once)

    assert len(once[0].expected_outcomes) == 1
    assert twice[0].expected_outcomes == once[0].expected_outcomes
