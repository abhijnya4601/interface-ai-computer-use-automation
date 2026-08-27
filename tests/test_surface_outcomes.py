"""
Offline unit tests for surface/outcomes.py — the per-target runtime/exceptional-state taxonomy
that replaces the take-home's per-capability-id _KNOWN_OUTCOMES dict. Live verification of the
full inject matrix is in DECISIONS.md D40 / the evidence/ replay_*inj* files.
"""
from artifact.schema import TargetSpec
from surface.outcomes import classify, profile_for

MERIDIAN = TargetSpec(
    app_name="meridian-core",
    entry_point="https://web-sample.interface-hiring.com/members",
    surface_type="legacy_web",
)
TAKEHOME = TargetSpec(
    app_name="mock-core-banking",
    entry_point="http://localhost:5050/search",
    surface_type="legacy_web",
)


def test_profile_for_known_host_loads_meridian_profile():
    prof = profile_for(MERIDIAN)
    assert prof and "http_status" in prof and "body_conditions" in prof


def test_profile_for_unknown_host_is_none():
    assert profile_for(TAKEHOME) is None


def test_no_profile_classifies_nothing():
    assert classify(None, 500, "anything") is None


def test_http_status_map_business_recoverable_hard():
    prof = profile_for(MERIDIAN)
    assert classify(prof, 404, "").classification == "business_outcome"
    assert classify(prof, 404, "").code == "RECORD_NOT_FOUND"
    assert classify(prof, 400, "").code == "VALIDATION_REJECTED"
    assert classify(prof, 403, "").classification == "business_outcome"
    assert classify(prof, 440, "").classification == "recoverable"
    assert classify(prof, 503, "").classification == "recoverable"
    assert classify(prof, 500, "").classification == "hard_failure"


def test_unmapped_status_and_2xx_fall_through():
    prof = profile_for(MERIDIAN)
    assert classify(prof, 200, "a normal page") is None
    assert classify(prof, 418, "teapot") is None


def test_body_condition_wins_over_generic_status():
    prof = profile_for(MERIDIAN)
    # MERIDIAN returns 400 for both HOLD-source and overdraw; the specific reason must win
    hold = classify(prof, 400, "... Source share is HOLD and cannot be debited ...")
    assert hold.code == "SOURCE_SHARE_ON_HOLD"
    over = classify(prof, 400, "... Insufficient available balance ...")
    assert over.code == "INSUFFICIENT_FUNDS"


def test_body_condition_matched_with_no_status():
    prof = profile_for(MERIDIAN)
    out = classify(prof, None, "... No member records matched your search ...")
    assert out.code == "MEMBER_NOT_FOUND" and out.classification == "business_outcome"


def test_supervisor_override_body_is_a_business_outcome():
    prof = profile_for(MERIDIAN)
    out = classify(prof, 403, "RESTRICTED FUNCTION - SUPERVISOR OVERRIDE REQUIRED")
    assert out.code == "SUPERVISOR_OVERRIDE_REQUIRED"
