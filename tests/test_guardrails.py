import pytest

from guardrails.policy import GuardrailViolation, check_risk_confirmation, guardrail_check, redact


def test_allowed_action_on_allowed_domain_passes():
    guardrail_check({"type": "click"}, current_url="http://localhost:5050/search")


def test_navigate_to_allowed_domain_passes():
    guardrail_check({"type": "navigate", "url": "http://localhost:5050/member/12345"})


def test_action_type_outside_allowlist_raises():
    with pytest.raises(GuardrailViolation, match="action type"):
        guardrail_check({"type": "download_file"}, current_url="http://localhost:5050/search")


def test_navigate_to_disallowed_domain_raises():
    with pytest.raises(GuardrailViolation, match="domain"):
        guardrail_check({"type": "navigate", "url": "https://evil.example.com/steal"})


def test_click_on_page_from_disallowed_domain_raises():
    with pytest.raises(GuardrailViolation, match="domain"):
        guardrail_check({"type": "click"}, current_url="https://not-our-bank.example.com/x")


def test_blocked_route_raises_even_on_allowed_domain(monkeypatch):
    import guardrails.policy as policy

    monkeypatch.setitem(policy.ALLOWLIST, "blocked_routes", ["/admin"])
    with pytest.raises(GuardrailViolation, match="blocked_routes"):
        guardrail_check({"type": "navigate", "url": "http://localhost:5050/admin/wipe-db"})


def test_risky_capability_without_confirm_raises():
    with pytest.raises(GuardrailViolation, match="confirm=True"):
        check_risk_confirmation("risky", confirm=False)


def test_risky_capability_with_confirm_passes():
    check_risk_confirmation("risky", confirm=True)


def test_safe_capability_never_requires_confirm():
    check_risk_confirmation("safe", confirm=False)


def test_redact_masks_secret_like_keys_case_insensitively():
    raw = {"member_id": "12345", "SSN": "123-45-6789", "Account_Number": "999888777"}
    out = redact(raw)
    assert out["member_id"] == "12345"
    assert out["SSN"] == "***REDACTED***"
    assert out["Account_Number"] == "***REDACTED***"


def test_redact_recurses_into_nested_structures():
    raw = {"step": {"value": {"password": "hunter2"}, "notes": ["token=abc"]}}
    out = redact(raw)
    assert out["step"]["value"]["password"] == "***REDACTED***"
    # "token=abc" isn't SSN/card-number-shaped, so the value pass leaves it alone — the key-based
    # pass doesn't apply here either, since the key is "notes", not "token".
    assert out["step"]["notes"] == ["token=abc"]


def test_redact_does_not_mutate_input():
    raw = {"password": "hunter2"}
    redact(raw)
    assert raw["password"] == "hunter2"


# ---- structured-secret value redaction (regardless of key name) ------------------------------

def test_redact_masks_ssn_shaped_value_under_an_unrelated_key():
    raw = {"notes": "caller confirmed SSN is 123-45-6789 on file"}
    out = redact(raw)
    assert "123-45-6789" not in out["notes"]
    assert "REDACTED" in out["notes"]


def test_redact_masks_card_number_shaped_value_inside_a_list():
    raw = {"log_lines": ["charged card 4111 1111 1111 1111 successfully"]}
    out = redact(raw)
    assert "4111 1111 1111 1111" not in out["log_lines"][0]


def test_redact_does_not_flag_a_short_member_id():
    raw = {"member_id": "12345"}
    out = redact(raw)
    assert out["member_id"] == "12345"


def test_redact_does_not_flag_a_currency_formatted_balance():
    raw = {"savings_balance": "$1,842.30"}
    out = redact(raw)
    assert out["savings_balance"] == "$1,842.30"


def test_redact_does_not_flag_a_customer_name():
    """Names aren't secret-shaped and legitimately belong in a capability's declared outputs
    (see guardrails/policy.py's module docstring) — full PII-name detection is an explicit,
    documented cut, not something value-shape redaction attempts."""
    raw = {"member_name": "Dana Whitfield"}
    out = redact(raw)
    assert out["member_name"] == "Dana Whitfield"
