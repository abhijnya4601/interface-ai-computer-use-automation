"""
Offline tests for the lease flip mechanism in escalation/controller.py. Uses a fake Playwright
page (just .url and .screenshot(path=...)) so this runs without a browser; trigger_escalation
itself is exercised end-to-end including the actual blocking-poll-then-resume behavior, just
with a background thread standing in for a human clicking "Resume" on the operator page.
"""
import threading
import time

import pytest

from escalation import controller
from escalation.lease import Lease


class FakePage:
    def __init__(self, url="http://localhost:5050/search"):
        self.url = url
        self.evaluate_calls: list = []

    def screenshot(self, path):
        with open(path, "wb") as f:
            f.write(b"fake-png-bytes")

    def evaluate(self, script, arg=None):
        self.evaluate_calls.append((script, arg))


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(controller, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(controller, "LEASE_PATH", tmp_path / "state" / "lease.json")
    monkeypatch.setattr(controller, "RESUME_SIGNAL_PATH", tmp_path / "state" / "resume.signal")
    monkeypatch.setattr(controller, "EVIDENCE_DIR", tmp_path / "evidence")
    yield


def test_initial_lease_defaults_to_automation():
    lease = controller.read_lease()
    assert lease.state == "automation"
    assert lease.context == {}


def test_trigger_escalation_flips_lease_to_human_and_blocks_until_resumed():
    page = FakePage()
    results = {}

    def _do_escalation():
        results["lease"] = controller.trigger_escalation(
            "dead-end: 3 consecutive turns with no state change", page, run_id="test_run",
            poll_interval_s=0.05, max_wait_s=5,
        )

    thread = threading.Thread(target=_do_escalation)
    thread.start()

    # give trigger_escalation a moment to actually flip the lease before we check + resume
    time.sleep(0.2)
    mid_lease = controller.read_lease()
    assert mid_lease.state == "human"
    assert mid_lease.context["reason"] == "dead-end: 3 consecutive turns with no state change"
    assert mid_lease.context["current_url"] == page.url

    controller.signal_resume(human_actions_summary="clicked through the confirmation manually")
    thread.join(timeout=5)

    assert results["lease"].state == "automation"
    assert controller.read_lease().state == "automation"


def test_trigger_escalation_writes_evidence_files():
    page = FakePage()
    thread = threading.Thread(
        target=lambda: controller.trigger_escalation(
            "stuck", page, run_id="evidence_test", poll_interval_s=0.05, max_wait_s=5
        )
    )
    thread.start()
    time.sleep(0.2)
    controller.signal_resume()
    thread.join(timeout=5)

    screenshot_path = controller.EVIDENCE_DIR / "escalation_evidence_test.png"
    context_path = controller.EVIDENCE_DIR / "escalation_evidence_test_context.json"
    assert screenshot_path.exists()
    assert context_path.exists()


def test_trigger_escalation_raises_timeout_if_never_resumed():
    page = FakePage()
    with pytest.raises(TimeoutError):
        controller.trigger_escalation("stuck", page, run_id="never", poll_interval_s=0.05, max_wait_s=0.2)


def test_resume_clears_signal_file_and_carries_no_decision_when_none_given():
    controller._write_lease(Lease(state="human", context={"reason": "x"}))
    controller.RESUME_SIGNAL_PATH.parent.mkdir(exist_ok=True)
    controller.RESUME_SIGNAL_PATH.write_text("{}")

    lease = controller.resume()
    assert lease.state == "automation"
    assert lease.context["decision"] is None
    assert not controller.RESUME_SIGNAL_PATH.exists()


def test_signal_resume_approved_decision_carries_through_to_resume():
    controller._write_lease(Lease(state="human", context={"reason": "risky step pending"}))
    controller.signal_resume(human_actions_summary="looks fine, go ahead", decision="approved")

    lease = controller.resume()
    assert lease.context["decision"] == "approved"
    assert lease.context["human_actions_summary"] == "looks fine, go ahead"


def test_signal_resume_declined_decision_carries_through_to_resume():
    controller._write_lease(Lease(state="human", context={"reason": "risky step pending"}))
    controller.signal_resume(human_actions_summary="do not proceed", decision="declined")

    lease = controller.resume()
    assert lease.context["decision"] == "declined"


def test_trigger_escalation_return_value_carries_the_operator_decision():
    page = FakePage()
    results = {}

    def _do_escalation():
        results["lease"] = controller.trigger_escalation(
            "risky step needs confirmation", page, run_id="decision_test",
            poll_interval_s=0.05, max_wait_s=5,
        )

    thread = threading.Thread(target=_do_escalation)
    thread.start()
    time.sleep(0.2)
    controller.signal_resume(human_actions_summary="approved after review", decision="approved")
    thread.join(timeout=5)

    assert results["lease"].context["decision"] == "approved"
    assert results["lease"].context["human_actions_summary"] == "approved after review"


def test_trigger_escalation_injects_a_pause_banner_before_the_screenshot():
    """A human watching the automation's own browser window (not the operator console) had no
    on-page signal it was paused -- only the terminal said so. The banner has to land before the
    evidence screenshot is taken, so the screenshot itself shows what a paused run looks like."""
    page = FakePage()
    thread = threading.Thread(
        target=lambda: controller.trigger_escalation(
            "risky step needs confirmation", page, run_id="banner_test",
            poll_interval_s=0.05, max_wait_s=5,
        )
    )
    thread.start()
    time.sleep(0.2)

    injection_calls = [c for c in page.evaluate_calls if isinstance(c[1], list)]
    assert len(injection_calls) == 1
    banner_id, reason_text = injection_calls[0][1]
    assert banner_id == controller._BANNER_ID
    assert reason_text == "risky step needs confirmation"
    assert "aria-hidden" in injection_calls[0][0]  # must never leak into the model's own perception

    controller.signal_resume(decision="approved")
    thread.join(timeout=5)


def test_trigger_escalation_removes_the_pause_banner_on_resume():
    page = FakePage()
    thread = threading.Thread(
        target=lambda: controller.trigger_escalation(
            "stuck", page, run_id="banner_removal_test", poll_interval_s=0.05, max_wait_s=5,
        )
    )
    thread.start()
    time.sleep(0.2)
    controller.signal_resume()
    thread.join(timeout=5)

    removal_calls = [c for c in page.evaluate_calls if c[1] == controller._BANNER_ID]
    assert len(removal_calls) == 1


def test_pause_banner_injection_never_raises_even_if_the_page_cant_be_evaluated():
    """Best-effort only -- a page mid-navigation or otherwise unevaluable must never break a
    real escalation over a cosmetic banner."""
    class BrokenPage(FakePage):
        def evaluate(self, script, arg=None):
            raise RuntimeError("page is mid-navigation")

    controller._inject_pause_banner(BrokenPage(), "some reason")
    controller._remove_pause_banner(BrokenPage())
