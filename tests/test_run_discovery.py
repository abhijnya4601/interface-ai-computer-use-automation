"""
Offline tests for scripts/run_discovery.py's pure helper logic - _inferred_checkpoint,
_infer_risk_level, and _console_watcher_step - none need a browser or API key.
"""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.run_discovery import (
    _console_watcher_step,
    _infer_risk_level,
    _inferred_checkpoint,
    _port_is_open,
    _resolve_checkpoint,
)


def test_inferred_checkpoint_uses_final_path_segment_when_url_changed():
    cp = _inferred_checkpoint(
        final_url="http://localhost:5050/member/12345/transactions",
        target_url="http://localhost:5050/search",
    )
    assert cp.type == "url_match"
    assert cp.expected == "transactions"


def test_inferred_checkpoint_final_segment_matches_a_differently_parameterized_url():
    """The whole point: the checkpoint built from one member_id's final URL must still
    correctly match a replay against a different member_id."""
    cp = _inferred_checkpoint(
        final_url="http://localhost:5050/member/12345/transactions",
        target_url="http://localhost:5050/search",
    )
    replay_final_url = "http://localhost:5050/member/23456/transactions"
    assert cp.expected in replay_final_url


def test_inferred_checkpoint_falls_back_to_full_url_if_page_never_navigated():
    cp = _inferred_checkpoint(
        final_url="http://localhost:5050/search",
        target_url="http://localhost:5050/search",
    )
    assert cp.expected == "http://localhost:5050/search"


def test_resolve_checkpoint_prefers_the_curated_yaml_entry_over_inference():
    # lookup_member_balance has a curated element_present checkpoint in
    # app_knowledge/mock-core-banking.yaml; that must win over the url-segment inference.
    cp = _resolve_checkpoint(
        "mock-core-banking", "lookup_member_balance",
        final_url="http://localhost:5050/member/12345", target_url="http://localhost:5050/search",
    )
    assert cp.type == "element_present"
    assert cp.provenance == "curated"


def test_resolve_checkpoint_falls_back_to_inference_for_an_unknown_capability():
    cp = _resolve_checkpoint(
        "mock-core-banking", "brand_new_capability",
        final_url="http://localhost:5050/x/y/results", target_url="http://localhost:5050/search",
    )
    assert cp.type == "url_match"
    assert cp.expected == "results"


def test_infer_risk_level_uses_the_curated_yaml_value_regardless_of_transcript():
    assert _infer_risk_level("open_subaccount", []) == "risky"
    assert _infer_risk_level("lookup_member_balance", [{"type": "escalate_requested"}]) == "safe"


def test_infer_risk_level_defaults_to_safe_when_no_escalation_occurred():
    transcript = [{"type": "navigate"}, {"type": "finish", "input": {"success": True}}]
    assert _infer_risk_level("some_new_capability", transcript) == "safe"


def test_infer_risk_level_defaults_to_risky_when_capability_is_uncurated_but_it_escalated():
    """A capability with no curated risk_level whose own discovery run needed a human sign-off
    has no business defaulting to safe - that default is what would let replay execute it later
    with zero --confirm gate."""
    transcript = [
        {"type": "navigate"},
        {"type": "escalate_requested", "reason": "state-changing action"},
        {"type": "escalation_resumed", "decision": "approved"},
        {"type": "finish", "input": {"success": True}},
    ]
    assert _infer_risk_level("an_uncurated_capability", transcript) == "risky"


def test_port_is_open_true_for_a_socket_actually_listening():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("localhost", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert _port_is_open("localhost", port) is True
    finally:
        srv.close()


def test_port_is_open_false_for_a_port_nothing_is_listening_on():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("localhost", 0))
    port = s.getsockname()[1]
    s.close()  # freed immediately -- nothing listening on it now
    assert _port_is_open("localhost", port) is False


def test_console_watcher_opens_once_when_lease_flips_to_human():
    """An operator playing the banker role has no way to know a run escalated unless they're
    watching the terminal -- --open-console-on-escalation should pop the console into their
    browser automatically, exactly once per escalation, not once per poll tick."""
    should_open, already_opened = _console_watcher_step("human", already_opened=False)
    assert should_open is True
    assert already_opened is True


def test_console_watcher_does_not_reopen_while_still_pending():
    should_open, already_opened = _console_watcher_step("human", already_opened=True)
    assert should_open is False
    assert already_opened is True


def test_console_watcher_rearms_after_resolution_for_a_second_escalation():
    should_open, already_opened = _console_watcher_step("automation", already_opened=True)
    assert should_open is False
    assert already_opened is False
    # and the very next poll, a fresh escalation should open it again
    should_open, already_opened = _console_watcher_step("human", already_opened=already_opened)
    assert should_open is True
    assert already_opened is True


def test_console_watcher_stays_quiet_when_nothing_is_escalated():
    should_open, already_opened = _console_watcher_step("automation", already_opened=False)
    assert should_open is False
    assert already_opened is False
