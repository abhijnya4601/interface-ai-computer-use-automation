import json
import logging

from common.logging import current_run_id, get_logger, run_context


def _capture(caplog):
    return [json.loads(r.getMessage()) if r.getMessage().startswith("{") else r.getMessage()
            for r in caplog.records]


def test_emits_json_with_event_and_fields(caplog):
    log = get_logger("test")
    with caplog.at_level(logging.INFO):
        log.info("step_done", step_id="s4", tier="role_name", ms=120)
    # the formatter runs on emit; assert on the structured record instead
    rec = caplog.records[-1]
    assert rec.getMessage() == "step_done"
    assert rec.fields == {"step_id": "s4", "tier": "role_name", "ms": 120}


def test_run_context_binds_and_unbinds_run_id():
    assert current_run_id() is None
    with run_context("replay_abc"):
        assert current_run_id() == "replay_abc"
        with run_context("nested"):
            assert current_run_id() == "nested"
        assert current_run_id() == "replay_abc"
    assert current_run_id() is None


def test_formatter_includes_run_id_when_bound():
    from common.logging import _JsonFormatter

    fmt = _JsonFormatter()
    rec = logging.LogRecord("r", logging.INFO, __file__, 1, "an_event", None, None)
    rec.fields = {"k": "v"}
    with run_context("run_xyz"):
        out = json.loads(fmt.format(rec))
    assert out["event"] == "an_event"
    assert out["run_id"] == "run_xyz"
    assert out["k"] == "v"
    assert out["level"] == "info"
