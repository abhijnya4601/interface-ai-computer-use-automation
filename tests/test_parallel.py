import threading
import time

from artifact.schema import Result
from replay.parallel import replay_many


def _make_stub(record_concurrency=None, lock=None, inflight=None):
    def _stub(capability, params, *, confirm=False, headless=True, run_id=None):
        if record_concurrency is not None:
            with lock:
                inflight[0] += 1
                record_concurrency[0] = max(record_concurrency[0], inflight[0])
            time.sleep(0.02)
            with lock:
                inflight[0] -= 1
        return Result(status="success", outputs={"echo": params["member_id"]})
    return _stub


def test_results_are_in_input_order_even_though_they_finish_out_of_order():
    def _stub(capability, params, *, confirm=False, headless=True, run_id=None):
        # earlier items sleep longer, so completion order is reversed
        time.sleep(0.05 - 0.01 * int(params["member_id"]))
        return Result(status="success", outputs={"echo": params["member_id"]})

    param_sets = [{"member_id": str(i)} for i in range(4)]
    results = replay_many(None, param_sets, concurrency=4, _replay_fn=_stub)
    assert [r.outputs["echo"] for r in results] == ["0", "1", "2", "3"]


def test_concurrency_cap_is_respected():
    peak = [0]
    inflight = [0]
    lock = threading.Lock()
    stub = _make_stub(peak, lock, inflight)
    replay_many(None, [{"member_id": str(i)} for i in range(10)], concurrency=3, _replay_fn=stub)
    assert peak[0] <= 3
    assert peak[0] >= 2  # actually parallelised, not silently serialised


def test_one_row_raising_does_not_sink_the_batch():
    def _stub(capability, params, *, confirm=False, headless=True, run_id=None):
        if params["member_id"] == "2":
            raise RuntimeError("boom")
        return Result(status="success", outputs={"echo": params["member_id"]})

    results = replay_many(None, [{"member_id": str(i)} for i in range(4)], _replay_fn=_stub)
    assert [r.status for r in results] == ["success", "success", "hard_failure", "success"]
    assert "boom" in results[2].failure_detail["observed"]


def test_empty_param_sets_returns_empty():
    assert replay_many(None, [], _replay_fn=_make_stub()) == []


# ---- warm pool path (concurrency == 1) ----------------------------------------------------

class _FakePool:
    """Stands in for engine.BrowserPool: records that ONE pool is opened for the whole batch
    and that runs are sequential."""
    instances = 0

    def __init__(self, headless=True):
        _FakePool.instances += 1
        self.runs = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, capability, params, *, confirm=False, run_id=None, idempotency_key=None):
        self.runs.append(params["member_id"])
        return Result(status="success", outputs={"echo": params["member_id"]})


def test_concurrency_1_uses_one_warm_pool_for_the_whole_batch():
    _FakePool.instances = 0
    holder = {}

    def _factory(headless=True):
        holder["pool"] = _FakePool(headless=headless)
        return holder["pool"]

    out = replay_many(None, [{"member_id": str(i)} for i in range(5)],
                      concurrency=1, _pool_factory=_factory)
    assert [r.outputs["echo"] for r in out] == ["0", "1", "2", "3", "4"]
    assert _FakePool.instances == 1                      # browser launched once, not 5x
    assert holder["pool"].runs == ["0", "1", "2", "3", "4"]  # in order


def test_pool_path_captures_a_bad_row_as_hard_failure():
    class _BoomPool(_FakePool):
        def run(self, capability, params, **kw):
            if params["member_id"] == "2":
                raise RuntimeError("boom")
            return Result(status="success", outputs={"echo": params["member_id"]})

    out = replay_many(None, [{"member_id": str(i)} for i in range(4)],
                      concurrency=1, _pool_factory=lambda headless=True: _BoomPool())
    assert [r.status for r in out] == ["success", "success", "hard_failure", "success"]
