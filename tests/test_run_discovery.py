"""
Offline test for scripts/run_discovery.py's _default_checkpoint (D22) — the fallback checkpoint
used for any capability_id not explicitly listed in CHECKPOINTS. Pure string logic, no browser
or API key needed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.run_discovery import _default_checkpoint


def test_default_checkpoint_uses_final_path_segment_when_url_changed():
    cp = _default_checkpoint(
        final_url="http://localhost:5050/member/12345/transactions",
        target_url="http://localhost:5050/search",
    )
    assert cp.type == "url_match"
    assert cp.expected == "transactions"


def test_default_checkpoint_final_segment_matches_a_differently_parameterized_url():
    """The whole point: the checkpoint built from one member_id's final URL must still
    correctly match a replay against a different member_id."""
    cp = _default_checkpoint(
        final_url="http://localhost:5050/member/12345/transactions",
        target_url="http://localhost:5050/search",
    )
    replay_final_url = "http://localhost:5050/member/23456/transactions"
    assert cp.expected in replay_final_url


def test_default_checkpoint_falls_back_to_full_url_if_page_never_navigated():
    cp = _default_checkpoint(
        final_url="http://localhost:5050/search",
        target_url="http://localhost:5050/search",
    )
    assert cp.expected == "http://localhost:5050/search"
