"""
`replay_many` — run one capability against many parameter sets, without paying the per-run
browser launch cost every time.

Two modes:
  - `concurrency == 1` (default): a single warm `BrowserPool` — one Chromium, an isolated
    `new_context()` per run, reused. A 400-member batch launches the browser once instead of
    400 times. Sequential (Playwright's sync API isn't safe to share across threads).
  - `concurrency > 1`: a thread pool, each worker calling `replay()` with its own
    Playwright + browser (the supported pattern for parallel sync Playwright). Faster wall
    time, at the cost of `concurrency` browsers' memory.

Ordering of the returned list matches `param_sets`; a run that raises is captured as its own
`Result(status="hard_failure", ...)`, never propagated, so one bad row can't sink the batch.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from artifact.schema import Capability, Result
from replay.engine import BrowserPool, replay

DEFAULT_CONCURRENCY = 1


def _as_failure(exc: BaseException) -> Result:
    return Result(
        status="hard_failure",
        failure_detail={"step_id": None, "expected": "replay to complete",
                        "observed": f"{type(exc).__name__}: {exc}"},
    )


def replay_many(
    capability: Capability,
    param_sets: list[dict],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    confirm: bool = False,
    headless: bool = True,
    _replay_fn: Callable[..., Result] | None = None,
    _pool_factory: Callable[..., BrowserPool] = BrowserPool,
) -> list[Result]:
    """Replay `capability` once per entry in `param_sets`. Results are returned in input order.
    `_replay_fn` / `_pool_factory` are test seams."""
    if not param_sets:
        return []

    # concurrency 1 -> warm pool, sequential. (skip the pool if a _replay_fn stub is injected)
    if concurrency <= 1 and _replay_fn is None:
        with _pool_factory(headless=headless) as pool:
            out = []
            for i, params in enumerate(param_sets):
                try:
                    out.append(pool.run(capability, params, confirm=confirm,
                                        run_id=f"replay_batch_{i}"))
                except Exception as exc:  # a bad row must not sink the batch
                    out.append(_as_failure(exc))
            return out

    # concurrency > 1 (or a stubbed replay fn) -> thread pool, browser per worker
    replay_fn = _replay_fn or replay
    concurrency = max(1, min(concurrency, len(param_sets)))
    results: list[Result | None] = [None] * len(param_sets)
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(replay_fn, capability, params, confirm=confirm, headless=headless,
                        run_id=f"replay_batch_{i}"): i
            for i, params in enumerate(param_sets)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = future.result()
            except Exception as exc:
                results[i] = _as_failure(exc)
    return [r for r in results if r is not None]
