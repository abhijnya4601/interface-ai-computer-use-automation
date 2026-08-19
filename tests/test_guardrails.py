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
    # list items aren't dict keys, so string content inside a list is left alone —
    # redact only masks by *key*, never scans string values for secret-shaped substrings.
    assert out["step"]["notes"] == ["token=abc"]


def test_redact_does_not_mutate_input():
    raw = {"password": "hunter2"}
    redact(raw)
    assert raw["password"] == "hunter2"
