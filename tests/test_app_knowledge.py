import app_knowledge


def test_loads_curated_rules_for_the_mock_bank():
    k = app_knowledge.load("mock-core-banking")
    rules = k.outcome_rules("lookup_member_balance")
    codes = {r["outcome"].code for r in rules}
    assert "MEMBER_NOT_FOUND" in codes and "PERMISSION_DENIED" in codes
    assert all(r["outcome"].provenance == "curated" for r in rules)


def test_curated_extract_contract_has_the_currency_shape():
    k = app_knowledge.load("mock-core-banking")
    contracts = k.extract_contracts("lookup_member_balance")
    assert "savings_balance" in contracts
    assert contracts["savings_balance"].provenance == "curated"
    import re
    assert re.fullmatch(contracts["savings_balance"].pattern, "$1,842.30")


def test_capability_config_carries_risk_and_checkpoint():
    k = app_knowledge.load("mock-core-banking")
    cfg = k.capability_config("open_subaccount")
    assert cfg.risk_level == "risky"
    assert cfg.checkpoint.type == "text_match"
    assert cfg.checkpoint.provenance == "curated"


def test_unknown_app_is_not_an_error_just_empty():
    k = app_knowledge.load("some-app-nobody-has-onboarded")
    assert k.outcome_rules("whatever") == []
    assert k.extract_contracts("whatever") == {}
    assert k.capability_config("whatever").risk_level is None


def test_unknown_capability_within_a_known_app_is_empty():
    k = app_knowledge.load("mock-core-banking")
    assert k.outcome_rules("not_a_real_capability") == []
