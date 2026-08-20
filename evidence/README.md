# /evidence/ — what's here and where to start

This directory has grown to 74 files across many discovery/replay/escalation runs made while
building and hardening this system (`../DECISIONS.md` has the full story behind each one). If
you're reviewing rather than exploring everything, start with the four files below — a real,
end-to-end, traceable example: one discovery run, the artifact it compiled, a clean replay, and a
replay that hits an exceptional state.

## The curated example

1. **`discovery_run_150cea0c47.jsonl`** — a real discovery run: goal `"Look up member 12345 and
   read their current savings balance."` against the live mock app. Full turn-by-turn transcript
   (LLM tool calls, observations, the final `finish()`), redacted, exactly as produced.
2. **`example_artifact_lookup_member_balance.v1.json`** — the `Capability` that run compiled to.
   A snapshot as of this submission; the live/canonical copy (which later runs may update) is
   always `../capabilities/lookup_member_balance.v1.json`.
3. **`replay_lookup_member_balance_member_id-23456.json`** — that artifact replayed
   deterministically (no LLM) against member `23456`, never seen during the discovery run above:
   `status: success`, `savings_balance: "$5.02"` — proof the artifact generalizes, not just
   replays back what it recorded.

## A replay that hits an exceptional state

Per the "ideally include one" ask, there are several, each a different kind:

- **`replay_lookup_hard_failure_bad_route.json`** — an **injected failure**: the capability's
  target was pointed at a route that doesn't resolve. `status: hard_failure`, with `step_id`,
  `expected`, `observed`, and a screenshot reference
  (`replay_replay_1787182364273_failure_s2.png`) — this is what an unrecoverable, unexpected
  break looks like end to end, not just a status code.
- **`replay_lookup_88888_not_found.json`** — a **bad/not-found input**: member `88888` was never
  seeded. `status: business_outcome`, `business_outcome_code: MEMBER_NOT_FOUND` — a real,
  expected outcome, not a crash.
- **`replay_lookup_99999_permission_denied.json`** — a **restricted-access input**: member `99999`
  is locked. `business_outcome_code: PERMISSION_DENIED`, same non-crash handling.
- **`replay_open_subaccount_no_confirm_blocked.json`** — a **bad call, not a bad input**: the
  risky `open_subaccount` capability replayed without `--confirm`. `status: hard_failure`, refused
  before the browser even launches.

## Stretch goal: an AI agent invoking a capability by name

- **`agent_capability_interface_demo_1787218791.json`** — a real Claude API call given the tool
  catalog built from `../capabilities/` (`agent_interface/catalog.py`); it chose to call
  `lookup_member_balance` for a member never mentioned in its own description, the real
  deterministic replay engine executed it (no LLM in that path), and Claude answered correctly.
  See `REPORT.md` §8 / `DECISIONS.md` D27 for the full design reasoning.
- **`agent_capability_interface_demo_1787218696.json`** — the *first* run of the same script,
  before the D27 fix: Claude declined to call the tool at all, misreading its description as
  hardcoded to one member. Kept deliberately, not deleted, as real evidence of the bug the live
  run found and the fix that followed.

## Everything else, briefly

- **`discovery_run_*.jsonl`** (16 total) — every real discovery run made during this build,
  across all 5 capabilities in `../capabilities/` plus throwaway/edge-case probes.
- **`replay_*.json`** (22) — every real replay result, across success/business-outcome/
  hard-failure for every capability and several member IDs each.
- **`escalation_run_*.png` / `*_context.json`** — a real screenshot + structured context
  (reason, URL, run ID) captured at the exact moment each real escalation paused for a human,
  including the interactive ones approved by hand through `escalation/operator_page.py`.
- **`escalation_demo_sequence.json`** — the fully-automated escalation demo's structured record
  (`scripts/demo_escalation.py`): real browser, real second operator process, real HTTP calls.
- **`phase6_guardrail_violation.json`** — a real out-of-allowlist action blocked by
  `guardrail_check`, with the halted transcript.
- **`lookup_member_balance_INJECTED_BAD_ROUTE.v1.json`** — the deliberately-broken capability
  variant used to produce the injected hard-failure replay above.

A short screen recording was considered and left out — every claim above is backed by a real,
inspectable file instead, which is more useful for line-by-line review than a video would be.
