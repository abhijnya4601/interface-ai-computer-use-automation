"""
agent_interface/assistant.py -- the reusable plan -> reuse-or-discover -> merge core.

The planning/phrasing LLM calls are stubbed with a fake session; `run_capability` and
`discover` are stubbed too, so this exercises the orchestration only (task order, output
merge, interleaved discovery, stop-on-failure, no-match), no browser or API key.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent.llm as llm
from agent_interface import assistant
from artifact.schema import Result


class _Turn:
    def __init__(self, tool_calls, text=""):
        self.tool_calls, self.text = tool_calls, text
        self.stop_reason, self.usage = "x", {"input": 1, "output": 1}


class _Session:
    """Turn 1 -> the plan (given). Every later complete() -> the phrasing text (given)."""
    provider, model, retry_exceptions = "fake", "fake-1", ()

    def __init__(self, plan_calls, phrase="done."):
        self._plan, self._phrase, self._n = plan_calls, phrase, 0

    def add_user_text(self, _t): pass
    def add_tool_results(self, _r): pass

    def complete(self, _system, _max, force_tool=True):
        self._n += 1
        return _Turn(self._plan, "") if self._n == 1 else _Turn([], self._phrase)


def _use_fake_session(monkeypatch, plan_calls, phrase="ok"):
    monkeypatch.setattr(assistant, "make_session", lambda **kw: _Session(plan_calls, phrase))
    # keep the real capability catalog names available
    monkeypatch.setattr(assistant, "build_tool_catalog", lambda: [])


def _ok(outputs):
    return Result(status="success", outputs=outputs)


def test_single_task_runs_one_capability_and_returns_its_output(monkeypatch):
    _use_fake_session(monkeypatch, [llm.ToolCall("c1", "lookup_member_balance", {"member_id": "12345"})])
    calls = []
    res = assistant.handle(
        "balance for 12345", api_key=None, provider=None,
        run_capability=lambda cap, args: calls.append((cap.capability_id, args)) or _ok({"savings_balance": "$1"}),
        discover=lambda g, n: (_ok({}), None, n))
    assert res.status == "success"
    assert res.outputs == {"savings_balance": "$1"}
    assert [t.task for t in res.tasks] == ["lookup_member_balance"]
    assert calls == [("lookup_member_balance", {"member_id": "12345"})]


def test_compound_request_runs_every_capability_in_order_and_merges(monkeypatch):
    _use_fake_session(monkeypatch, [
        llm.ToolCall("c1", "lookup_member_balance", {"member_id": "12345"}),
        llm.ToolCall("c2", "lookup_latest_transaction", {"member_id": "12345"}),
    ])
    order = []

    def _run(cap, args):
        order.append(cap.capability_id)
        return _ok({"savings_balance": "$1"} if cap.capability_id == "lookup_member_balance"
                   else {"most_recent_date": "2026-01-01"})

    res = assistant.handle("name and last txn for 12345", api_key=None, provider=None,
                           run_capability=_run, discover=lambda g, n: (_ok({}), None, n))
    assert order == ["lookup_member_balance", "lookup_latest_transaction"]
    assert res.outputs == {"savings_balance": "$1", "most_recent_date": "2026-01-01"}
    assert [t.task for t in res.tasks] == ["lookup_member_balance", "lookup_latest_transaction"]


def test_task_without_a_capability_falls_through_to_discovery(monkeypatch):
    _use_fake_session(monkeypatch, [
        llm.ToolCall("c1", "lookup_member_balance", {"member_id": "12345"}),
        llm.ToolCall("c2", "discover_new_capability", {"goal": "read the member status", "name": "lookup_status"}),
    ])
    discovered = []

    def _discover(goal, name):
        discovered.append((goal, name))
        return _ok({"member_status": "active"}), "capabilities/lookup_status.v1.json", name

    res = assistant.handle("balance then status for 12345", api_key=None, provider=None,
                           run_capability=lambda cap, args: _ok({"savings_balance": "$1"}),
                           discover=_discover)
    assert discovered == [("read the member status", "lookup_status")]
    assert res.outputs == {"savings_balance": "$1", "member_status": "active"}
    assert res.learned == ["capabilities/lookup_status.v1.json"]
    assert [t.task for t in res.tasks] == ["lookup_member_balance", "discover:lookup_status"]


def test_stops_at_the_first_task_that_does_not_cleanly_succeed(monkeypatch):
    _use_fake_session(monkeypatch, [
        llm.ToolCall("c1", "lookup_member_balance", {"member_id": "88888"}),
        llm.ToolCall("c2", "lookup_latest_transaction", {"member_id": "88888"}),
    ])
    ran = []

    def _run(cap, args):
        ran.append(cap.capability_id)
        if cap.capability_id == "lookup_member_balance":
            return Result(status="business_outcome", business_outcome_code="MEMBER_NOT_FOUND")
        return _ok({"most_recent_date": "x"})

    res = assistant.handle("x", api_key=None, provider=None, run_capability=_run,
                           discover=lambda g, n: (_ok({}), None, n))
    assert ran == ["lookup_member_balance"]                # second task never ran
    assert res.status == "business_outcome"
    assert res.business_outcome_code == "MEMBER_NOT_FOUND"


def test_no_tool_call_is_a_no_match_with_the_models_text(monkeypatch):
    _use_fake_session(monkeypatch, [])
    events = []
    res = assistant.handle("do a backflip", api_key=None, provider=None,
                           run_capability=lambda *a: _ok({}), discover=lambda g, n: (_ok({}), None, n),
                           on_event=events.append)
    assert res.status == "no_match"
    assert any(e.get("type") == "stop" for e in events)


def test_should_stop_halts_the_task_loop(monkeypatch):
    _use_fake_session(monkeypatch, [
        llm.ToolCall("c1", "lookup_member_balance", {"member_id": "1"}),
        llm.ToolCall("c2", "lookup_latest_transaction", {"member_id": "1"}),
    ])
    ran = []
    res = assistant.handle("x", api_key=None, provider=None,
                           run_capability=lambda cap, a: ran.append(cap.capability_id) or _ok({}),
                           discover=lambda g, n: (_ok({}), None, n),
                           should_stop=lambda: len(ran) >= 1)
    assert ran == ["lookup_member_balance"]
    assert len(res.tasks) == 1
