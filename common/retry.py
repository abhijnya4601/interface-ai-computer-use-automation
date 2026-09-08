"""
Retry with exponential backoff + full jitter, for the genuinely transient failures the system
currently has no answer for: a flaky network on an LLM call, a slow page, a transient 5xx from
the target app. Deliberately NOT for anything replay decides deterministically — a
`business_outcome` or `data_unavailable` is a real answer, never retried.

    from common.retry import retry_call

    resp = retry_call(
        lambda: client.messages.create(...),
        attempts=3, base_delay=1.0, retry_on=(anthropic.APIStatusError, anthropic.APIConnectionError),
        on_retry=lambda a, exc, delay: log.warning("llm_retry", attempt=a, err=str(exc), delay=delay),
    )
"""
from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from typing import TypeVar

T = TypeVar("T")


class RetryExhausted(RuntimeError):
    def __init__(self, attempts: int, last_exc: BaseException):
        super().__init__(f"gave up after {attempts} attempt(s): {type(last_exc).__name__}: {last_exc}")
        self.last_exc = last_exc


def backoff_delays(attempts: int, base_delay: float, max_delay: float) -> list[float]:
    """The full-jitter schedule for `attempts` tries: attempt i waits a random value in
    [0, min(max_delay, base_delay * 2**i)). Exposed so it can be asserted on in tests without
    real sleeps."""
    return [
        random.uniform(0, min(max_delay, base_delay * (2 ** i)))
        for i in range(attempts - 1)
    ]


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_on: Sequence[type[BaseException]] = (Exception,),
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    _sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `fn`; on an exception whose type is in `retry_on`, wait and try again, up to
    `attempts` total. Anything not in `retry_on` propagates immediately. Raises `RetryExhausted`
    (chained to the last error) when all attempts fail."""
    retry_on = tuple(retry_on)
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as exc:  # type: ignore[misc]
            last_exc = exc
            if attempt == attempts:
                break
            delay = random.uniform(0, min(max_delay, base_delay * (2 ** (attempt - 1))))
            if on_retry:
                on_retry(attempt, exc, delay)
            _sleep(delay)
    raise RetryExhausted(attempts, last_exc) from last_exc
