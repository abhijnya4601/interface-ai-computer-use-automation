import json

import pytest

from artifact.schema import Capability, Checkpoint, Step, TargetSpec
from replay import engine, idempotency


def _risky_cap():
    return Capability(
        capability_id="open_subaccount", version="1.0.0", created_from_run_id="r",
        target=TargetSpec(app_name="mock-core-banking", entry_point="http://x/"),
        risk_level="risky", input_schema={}, output_schema={},
        checkpoint=Checkpoint(type="url_match", expected="x"),
        steps=[Step(step_id="s1", action_type="navigate", value="http://x/")],
    )


def _safe_cap():
    return _risky_cap().model_copy(update={"risk_level": "safe", "capability_id": "lookup_x"})


# ---- ledger file ------------------------------------------------------------------------------

def test_record_then_lookup_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setattr(idempotency, "_LEDGER", tmp_path / "ledger.jsonl")
    assert idempotency.lookup("k1") is None
    idempotency.record("k1", "open_subaccount", {"status": "success"})
    entry = idempotency.lookup("k1")
    assert entry["capability_id"] == "open_subaccount"
    assert entry["result"] == {"status": "success"}


def test_lookup_is_none_for_an_unknown_key(tmp_path, monkeypatch):
    monkeypatch.setattr(idempotency, "_LEDGER", tmp_path / "ledger.jsonl")
    idempotency.record("k1", "c", {"status": "success"})
    assert idempotency.lookup("other") is None


def test_empty_key_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(idempotency, "_LEDGER", tmp_path / "ledger.jsonl")
    idempotency.record("", "c", {"status": "success"})
    assert not (tmp_path / "ledger.jsonl").exists()


# ---- replay() short-circuit (runs before any browser is launched) ---------------------------

def test_risky_replay_with_a_used_key_returns_the_recorded_outcome_without_executing(monkeypatch):
    stored = {"status": "business_outcome", "business_outcome_code": "SUBACCOUNT_OPENED",
              "outputs": {"sub_account_number": "7"}}
    monkeypatch.setattr(idempotency, "lookup", lambda key: {"result": stored} if key == "k9" else None)

    # sync_playwright would raise if reached with no browser installed in this context; the
    # point is it is NOT reached.
    monkeypatch.setattr(engine, "sync_playwright", lambda: (_ for _ in ()).throw(
        AssertionError("replay executed instead of using the idempotency ledger")))

    result = engine.replay(_risky_cap(), {}, confirm=True, idempotency_key="k9")
    assert result.status == "business_outcome"
    assert result.business_outcome_code == "SUBACCOUNT_OPENED"
    assert result.idempotent_replay is True


def test_safe_capability_is_never_ledgered_even_with_a_key(monkeypatch):
    called = {"lookup": False}

    def _lookup(key):
        called["lookup"] = True
        return None

    monkeypatch.setattr(idempotency, "lookup", _lookup)
    monkeypatch.setattr(engine, "sync_playwright", lambda: (_ for _ in ()).throw(RuntimeError("stop")))

    with pytest.raises(RuntimeError, match="stop"):  # got past idempotency, into the browser path
        engine.replay(_safe_cap(), {}, idempotency_key="k9")
    assert called["lookup"] is False


def test_json_is_valid_after_record(tmp_path, monkeypatch):
    monkeypatch.setattr(idempotency, "_LEDGER", tmp_path / "ledger.jsonl")
    idempotency.record("k", "c", {"status": "success", "trace": [{"ms": 1.5}]})
    line = (tmp_path / "ledger.jsonl").read_text().strip()
    assert json.loads(line)["key"] == "k"
