import json
from pathlib import Path

import pytest

from artifact.patch import apply_patch, patch_summary
from artifact.schema import Capability

BASE_PATH = Path(__file__).parent.parent / "capabilities" / "lookup_member_balance.v1.json"
ACME_PATCH = Path(__file__).parent.parent / "capabilities" / "tenants" / "acme.lookup_member_balance.patch.json"


def _base() -> Capability:
    return Capability.model_validate_json(BASE_PATH.read_text())


def test_apply_the_real_acme_patch_renames_only_the_balance_step_and_checkpoint():
    base = _base()
    patch = json.loads(ACME_PATCH.read_text())
    acme = apply_patch(base, patch)

    s5 = next(s for s in acme.steps if s.step_id == "s5")
    assert s5.target.primary["name"] == "Ledger Balance"
    assert s5.target.fallbacks == [{"strategy": "text", "text": "Ledger Balance"}]
    assert acme.checkpoint.locator["name"] == "Ledger Balance"

    # every other step is untouched, byte for byte
    for sid in ("s1", "s2", "s3", "s4"):
        assert next(s for s in acme.steps if s.step_id == sid) == next(s for s in base.steps if s.step_id == sid)
    # and the base object was not mutated
    assert next(s for s in base.steps if s.step_id == "s5").target.primary["name"] == "Savings Balance"


def test_patched_capability_is_draft_and_unverified():
    acme = apply_patch(_base(), json.loads(ACME_PATCH.read_text()))
    assert acme.lifecycle == "draft"
    assert acme.verification is None


def test_top_level_field_override_deep_merges():
    acme = apply_patch(_base(), {"tenant": "x", "target": {"entry_point": "https://x.example/search"}})
    assert acme.target.entry_point == "https://x.example/search"
    assert acme.target.app_name == "mock-core-banking"  # untouched sibling


def test_patch_targeting_an_unknown_step_id_is_rejected():
    with pytest.raises(KeyError, match="s99"):
        apply_patch(_base(), {"steps": {"s99": {"value": "x"}}})


def test_patch_that_would_break_the_schema_is_rejected():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        apply_patch(_base(), {"risk_level": "not_a_real_level"})


def test_patch_summary_lists_the_changes():
    summary = patch_summary(_base(), json.loads(ACME_PATCH.read_text()))
    assert any("checkpoint" in s for s in summary)
    assert any("step s5" in s for s in summary)
