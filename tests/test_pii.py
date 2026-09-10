"""
Gray-area redaction (guardrails/pii.py). These target the regex fallback so they pass in CI
without Presidio installed; the handful of PERSON/LOCATION assertions skip unless the Presidio
backend actually loaded.
"""
import pytest

from guardrails import pii
from guardrails.pii import RedactionReport, scrub_gray_area

_HAS_PRESIDIO = pii.BACKEND == "presidio"


# ---- sink routing ---------------------------------------------------------------------------

def test_llm_prompt_sink_hard_masks_an_email():
    out, report, token_map = scrub_gray_area({"note": "ping dana@example.com about it"}, "llm_prompt")
    assert "dana@example.com" not in out["note"]
    assert "<EMAIL_ADDRESS:REDACTED>" in out["note"]
    assert report.by_type.get("EMAIL_ADDRESS") == 1
    assert token_map == {}  # no reversible map for llm_prompt


def test_evidence_sink_tokenizes_reversibly_and_consistently():
    payload = {"a": "mail dana@example.com", "b": "again dana@example.com", "c": "other wei@bank.test"}
    out, _report, token_map = scrub_gray_area(payload, "evidence")
    assert out["a"] == "mail <EMAIL_ADDRESS_1>"
    assert out["b"] == "again <EMAIL_ADDRESS_1>"          # same value -> same token
    assert out["c"] == "other <EMAIL_ADDRESS_2>"
    assert token_map == {"dana@example.com": "<EMAIL_ADDRESS_1>", "wei@bank.test": "<EMAIL_ADDRESS_2>"}


def test_phone_number_is_detected():
    out, report, _ = scrub_gray_area({"x": "call (415) 555-0132 now"}, "llm_prompt")
    assert "555-0132" not in out["x"]
    assert report.by_type.get("PHONE_NUMBER") == 1


def test_street_address_is_detected_and_flagged_low_confidence():
    out, report, _ = scrub_gray_area({"x": "lives at 412 Birchwood Lane apt 2"}, "llm_prompt")
    assert "412 Birchwood Lane" not in out["x"]
    assert report.by_type.get("STREET_ADDRESS") == 1
    assert any(item["type"] == "STREET_ADDRESS" for item in report.low_confidence)


def test_bare_zip_is_flagged_not_masked_so_discovery_still_works():
    # masking bare numerics would break a computer-use agent that navigates by IDs - a lone
    # 5-digit run is left intact in every sink, but recorded on the report for review.
    out_ev, _, _ = scrub_gray_area({"x": "Madison WI 53703"}, "evidence")
    assert out_ev["x"] == "Madison WI 53703"
    out_llm, report, _ = scrub_gray_area({"x": "member 53703 lookup"}, "llm_prompt")
    assert out_llm["x"] == "member 53703 lookup"           # NOT masked
    assert any(f["kind"] == "ZIP-shaped" for f in report.flagged_not_masked)


def test_long_digit_run_is_flagged_not_masked():
    out_llm, report, _ = scrub_gray_area({"x": "ref 900112233445"}, "llm_prompt")
    assert out_llm["x"] == "ref 900112233445"
    assert any(f["kind"] == "long-digit-run" for f in report.flagged_not_masked)
    out_ev, _, _ = scrub_gray_area({"x": "ref 900112233445"}, "evidence")
    assert out_ev["x"] == "ref 900112233445"


def test_a_zip_inside_a_full_street_address_is_masked_as_part_of_the_address():
    out, report, _ = scrub_gray_area({"x": "at 412 Birchwood Lane, Madison WI 53703 today"}, "llm_prompt")
    assert "53703" not in out["x"]
    assert "Birchwood" not in out["x"]
    assert report.by_type.get("STREET_ADDRESS") == 1


def test_nested_structures_are_walked():
    payload = {"turns": [{"obs": "email a@b.com"}, {"obs": "clean"}]}
    out, _report, _ = scrub_gray_area(payload, "llm_prompt")
    assert out["turns"][0]["obs"] == "email <EMAIL_ADDRESS:REDACTED>"
    assert out["turns"][1]["obs"] == "clean"


def test_input_is_not_mutated():
    payload = {"x": "a@b.com"}
    scrub_gray_area(payload, "llm_prompt")
    assert payload["x"] == "a@b.com"


def test_non_string_leaves_pass_through():
    out, _, _ = scrub_gray_area({"n": 5, "ok": True, "none": None}, "llm_prompt")
    assert out == {"n": 5, "ok": True, "none": None}


# ---- report shape --------------------------------------------------------------------------

def test_report_as_dict_and_summary_line():
    _, report, _ = scrub_gray_area({"x": "a@b.com and c@d.com"}, "llm_prompt")
    d = report.as_dict()
    assert d["total_redacted"] == 2
    assert d["by_type"] == {"EMAIL_ADDRESS": 2}
    assert "2 redacted" in report.summary_line()


def test_empty_report_summary_line():
    _, report, _ = scrub_gray_area({"x": "nothing sensitive here"}, "llm_prompt")
    assert report.total == 0
    assert "nothing to redact" in report.summary_line()


# ---- Presidio-only -----------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_PRESIDIO, reason="Presidio not installed; PERSON needs NER")
def test_person_name_detected_with_presidio():
    out, report, _ = scrub_gray_area({"x": "the member is Dana Whitfield"}, "llm_prompt")
    assert "Dana Whitfield" not in out["x"]
    assert report.by_type.get("PERSON", 0) >= 1


def test_reported_backend_is_one_of_the_two_known_values():
    assert pii.BACKEND in ("presidio", "regex-fallback")
    assert RedactionReport(sink="evidence", backend=pii.BACKEND).backend == pii.BACKEND
