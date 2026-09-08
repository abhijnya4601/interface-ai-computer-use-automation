import pytest

from common.retry import RetryExhausted, backoff_delays, retry_call


def test_returns_on_first_success_without_sleeping():
    slept = []
    out = retry_call(lambda: 42, _sleep=slept.append)
    assert out == 42
    assert slept == []


def test_retries_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient")
        return "ok"

    slept = []
    out = retry_call(flaky, attempts=5, retry_on=(ConnectionError,), _sleep=slept.append)
    assert out == "ok"
    assert calls["n"] == 3
    assert len(slept) == 2  # slept before attempts 2 and 3


def test_non_retryable_exception_propagates_immediately():
    def boom():
        raise ValueError("nope")

    slept = []
    with pytest.raises(ValueError):
        retry_call(boom, retry_on=(ConnectionError,), _sleep=slept.append)
    assert slept == []


def test_exhaustion_raises_retryexhausted_chained_to_last_error():
    def always():
        raise TimeoutError("still down")

    with pytest.raises(RetryExhausted) as ei:
        retry_call(always, attempts=3, retry_on=(TimeoutError,), _sleep=lambda _: None)
    assert isinstance(ei.value.last_exc, TimeoutError)


def test_on_retry_callback_gets_attempt_and_exception():
    seen = []

    def flaky():
        raise ConnectionError("x")

    with pytest.raises(RetryExhausted):
        retry_call(
            flaky, attempts=3, retry_on=(ConnectionError,), _sleep=lambda _: None,
            on_retry=lambda a, exc, delay: seen.append((a, type(exc).__name__)),
        )
    assert seen == [(1, "ConnectionError"), (2, "ConnectionError")]


def test_backoff_delays_are_bounded_and_grow():
    delays = backoff_delays(attempts=5, base_delay=1.0, max_delay=8.0)
    assert len(delays) == 4
    assert all(0 <= d <= 8.0 for d in delays)
