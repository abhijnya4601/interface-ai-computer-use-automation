
from escalation import policy
from replay.engine import _precheck


def _ctx(**kw):
    base = {"capability_id": "open_subaccount", "risk_level": "risky",
            "target_app": "mock-core-banking",
            "step": {"action_type": "click", "name": "Confirm and Open Account"}, "params": {}}
    base.update(kw)
    return base


# ---- rule matching ------------------------------------------------------------------------

def test_no_rules_means_allow():
    assert policy.evaluate(_ctx(), rules=[]).action == "allow"


def test_risk_and_step_name_rule_escalates():
    rules = [{"id": "r1", "when": {"risk_level": "risky", "step_name_contains": ["Confirm"]},
              "then": "escalate"}]
    d = policy.evaluate(_ctx(), rules=rules)
    assert d.action == "escalate" and d.rule_id == "r1"


def test_step_name_is_case_insensitive_and_any_of():
    rules = [{"id": "r", "when": {"step_name_contains": ["submit", "open account"]}, "then": "escalate"}]
    assert policy.evaluate(_ctx(step={"action_type": "click", "name": "Open Account now"}),
                           rules=rules).action == "escalate"


def test_capability_id_can_be_a_list():
    rules = [{"id": "r", "when": {"capability_id": ["open_subaccount", "dispute_transaction"]},
              "then": "block"}]
    assert policy.evaluate(_ctx(), rules=rules).action == "block"
    assert policy.evaluate(_ctx(capability_id="lookup_member_balance"), rules=rules).action == "allow"


def test_step_action_filter():
    rules = [{"id": "r", "when": {"step_action": ["type"]}, "then": "escalate"}]
    assert policy.evaluate(_ctx(step={"action_type": "click", "name": "x"}), rules=rules).action == "allow"
    assert policy.evaluate(_ctx(step={"action_type": "type", "name": "x"}), rules=rules).action == "escalate"


def test_param_numeric_comparison():
    rules = [{"id": "big", "when": {"param": {"name": "opening_deposit", "op": "gt", "value": 500}},
              "then": "escalate"}]
    assert policy.evaluate(_ctx(params={"opening_deposit": "750"}), rules=rules).action == "escalate"
    assert policy.evaluate(_ctx(params={"opening_deposit": "50"}), rules=rules).action == "allow"
    assert policy.evaluate(_ctx(params={}), rules=rules).action == "allow"  # param absent


def test_first_matching_rule_wins():
    rules = [
        {"id": "a", "when": {"risk_level": "risky"}, "then": "allow"},
        {"id": "b", "when": {"step_name_contains": ["Confirm"]}, "then": "escalate"},
    ]
    d = policy.evaluate(_ctx(), rules=rules)
    assert d.action == "allow" and d.rule_id == "a"


def test_a_rule_that_needs_a_context_key_absent_in_discovery_just_does_not_match():
    # discovery has no capability_id / risk_level / params — a replay-shaped rule must not fire
    rules = [{"id": "r", "when": {"capability_id": "open_subaccount", "risk_level": "risky"},
              "then": "block"}]
    disc_ctx = {"capability_id": None, "risk_level": None, "target_app": None,
                "step": {"action_type": "click", "name": "Continue"}, "params": {}}
    assert policy.evaluate(disc_ctx, rules=rules).action == "allow"


# ---- file round-trip --------------------------------------------------------------------

def test_load_and_save_round_trip(tmp_path):
    p = tmp_path / "rules.yaml"
    rules = [{"id": "x", "description": "d", "when": {"risk_level": "risky"}, "then": "escalate"}]
    policy.save_rules(rules, path=p)
    assert policy.load_rules(path=p) == rules


def test_load_drops_rules_with_a_bogus_then(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text("rules:\n"
                 "  - {id: ok, when: {risk_level: risky}, then: escalate}\n"
                 "  - {id: bad, when: {risk_level: safe}, then: teleport}\n")
    ids = [r["id"] for r in policy.load_rules(path=p)]
    assert ids == ["ok"]


def test_missing_file_is_empty(tmp_path):
    assert policy.load_rules(path=tmp_path / "nope.yaml") == []


# ---- the shipped default rules ---------------------------------------------------------

def test_shipped_rules_escalate_a_risky_confirm_click():
    d = policy.evaluate(_ctx())  # uses escalation/rules.yaml
    assert d.action == "escalate"


def test_shipped_rules_leave_a_safe_lookup_alone():
    ctx = _ctx(capability_id="lookup_member_balance", risk_level="safe",
               step={"action_type": "click", "name": "View"})
    assert policy.evaluate(ctx).action == "allow"


# ---- precheck honours allow_escalation -------------------------------------------------

class _RiskyCap:
    risk_level = "risky"
    capability_id = "open_subaccount"


def test_precheck_still_refuses_risky_without_confirm_when_no_operator():
    short, _ = _precheck(_RiskyCap(), confirm=False, idempotency_key=None, allow_escalation=False)
    assert short is not None and short.status == "hard_failure"


def test_precheck_lets_risky_through_when_an_operator_can_approve():
    short, _ = _precheck(_RiskyCap(), confirm=False, idempotency_key=None, allow_escalation=True)
    assert short is None  # no up-front refusal; the per-step policy will pause it
