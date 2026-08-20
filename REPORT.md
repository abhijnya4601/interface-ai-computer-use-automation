# REPORT

## 1. Architecture

Single Python process, no queues, no services — the assignment explicitly penalizes premature
scaling infrastructure, and nothing here needs it. Six modules, each with one job:

- **`app/`** — the target: a mock legacy core-banking Flask/SQLite app with hostile markup
  (nested tables, non-semantic classes, zero test IDs) over real semantic HTML (`<button>`,
  `<label for>`, `<th scope=row>`), plus a real `<iframe>` boundary for the sub-account
  confirmation step — the stress case for "no clean DOM." Chosen over a desktop app (the
  assignment's other suggested option): a desktop surface needs OS-level accessibility automation
  as a second perception/action backend without adding a genuinely different *kind* of robustness
  question — the load-bearing pieces (schema, replay determinism, escalation) are
  surface-agnostic by design either way (section 4), so a web target let effort go there instead
  of into a second integration surface.
- **`agent/perception.py`** — turns whatever Playwright can see into a small
  `{role, name, value, children}` tree, merging content from inside iframes so the LLM never has
  to reason about frame boundaries.
- **`agent/{tools,discovery,recorder}.py`** — the live, LLM-driven observe→decide→act loop, and
  the code that turns each accepted action into a typed `Step` alongside it, not as a later pass
  over a transcript.
- **`agent/compiler.py`** + **`artifact/schema.py`** — turns a run's recorded steps into a
  versioned `Capability`, the contract everything downstream depends on.
- **`replay/engine.py`** — the deterministic, no-LLM production execution path.
- **`guardrails/`** and **`escalation/`** — cross-cutting: allowlist + redaction wired into both
  discovery and replay; a file-backed lease for human handoff.

**Trade-off — perception API.** The build brief specified `page.accessibility.snapshot()`, which
no longer exists in current Playwright (`AttributeError: 'Page' object has no attribute
'accessibility'`, verified directly). Rebuilt on `Locator.aria_snapshot()` (YAML text) instead,
with per-frame snapshotting to cross iframe boundaries — a top-level snapshot does *not* reach
iframe content by default (also verified directly, not assumed). Full story in `DECISIONS.md` D6.

**Trade-off — model.** `claude-sonnet-5`, not the largest available model: this is a production
capability-discovery agent for a bank, where cost/latency and reliably following one explicit
safety rule (escalate before irreversible actions) matter more than frontier reasoning on a task
this constrained.

## 2. Artifact schema

`artifact/schema.py` — the most heavily-invested piece. A `Capability` is: `capability_id`,
semver `version`, `created_from_run_id` (provenance), `target`, `risk_level` (`safe`/`risky`),
typed `input_schema`/`output_schema`, a `checkpoint`, and `steps: list[Step]`.

Each `Step` carries a `LocatorTarget` (`strategy`, `primary`, `fallbacks`, and a required
`reasoning` string — a locator with no stated reasoning is exactly the kind of unreviewable
artifact this schema exists to prevent), a `value` that's either a literal or
`{"param_ref": "member_id"}`, and — the load-bearing field — `expected_outcomes:
list[ExpectedOutcome]`, each `{condition, classification, code, handling}` with `classification`
one of `business_outcome` / `recoverable` / `hard_failure`. These are declared by
`agent/compiler.py` from domain knowledge of the target app (one discovery run only observes its
own happy path; finalizing a capability for production means documenting the branches you know
exist, same spirit as `reasoning`), and replay evaluates them as **literal, deterministic
checks** against the live page — never a guess, never an LLM call.

This exact design point produced the most interesting bug in this build (D14): a declared
`PERMISSION_DENIED` outcome was attached to the wrong step because it was reasoned from
server-side route logic instead of what the UI actually renders. The fix wasn't "trust the
schema less," it was "replay every declared branch against the real app" — which the schema's
shape makes checkable in the first place. A single unstructured `on_error` field wouldn't have
surfaced *which step* was wrong this clearly.

## 3. Determinism & error handling

Replay resolves every `Step.target` the same way the recorder declared it: tier 1 `role_name`
(unique role+accessible-name, backed by real semantic HTML, not CSS/IDs), tier 2 `structural`
(declared `nth` when role+name isn't unique), tier 3 `text` (raw text-content match, logged as a
warning). This app never needs tier 2/3 in practice, but both are real, exercised code paths
(`tests/test_recorder.py` proves tier selection with fake match counts). **The tier log doubles
as a free drift-detection signal**: rising tier-2/3 usage across replays means the UI drifted, at
zero extra infrastructure cost.

Each step's `expected_outcomes` are checked deterministically: when a declared locator can't be
resolved (or fails to act), and after a successful action, replay checks whether any declared
`condition` (a literal `"page contains '<substring>'"`) matches the live page. A match returns
`business_outcome` or `recoverable`; no match on a failure is `hard_failure`, returned with
`step_id`, `expected`, `observed`, and a screenshot reference. `wait_policy` retries are opt-in
per step (`retry_on: transient_load`), not blanket-applied. `recoverable` stops just as cleanly as
`business_outcome` rather than retrying in-place — a replay engine that silently retries an
unrecognized page state is the wrong instinct for a banking system; what it buys the *caller* is a
status distinct from `hard_failure` ("safe to retry the whole run later" vs. "something's broken,
investigate"). None of the 5 real capabilities declare one — this mock app's business logic is
fully deterministic with no naturally-occurring transient state, so it's exercised in
`tests/test_replay.py`, not live. Declaring one just to exercise the path would mean fabricating
non-deterministic app behavior, directly against this section's own goal.

All four required scenarios ran against real compiled capabilities, for both capabilities:
success with a never-recorded `member_id`, both business outcomes, and an injected hard failure
(target pointed at a nonexistent route) — real output saved to `/evidence/`.

**A fourth locator tier, `table_position` (D21→D22)**: found by extending the app past the two
required capabilities, into a UI shape neither one uses (a transaction-history table, no per-row
label). Discovery generalized to it with zero code changes — but the recorded locator anchored on
the extracted value itself, and replaying against a different member failed immediately, for
real. Fixed by addressing a cell via its table's column headers + row/column index instead of
content, verified live against two pages with identical structure and different data. The same
live run also caught the default checkpoint checking the final page against the *starting* URL
(backwards), and `output_schema` declaring outputs `finish()` reported with no step backing
them — both fixed, plus an empty-transactions edge case now correctly returns
`business_outcome`/`NO_TRANSACTIONS` instead of silently succeeding with placeholder text.
`lookup_latest_transaction` is now a third fully-verified capability, to the same standard as the
two the assignment requires.

**Proof this generalizes beyond three pre-built capabilities, not just three (D23):** pointed
discovery at two real app features with zero prior capability — `dispute_transaction`,
`update_member_address` — with fresh goal wording each time. Both succeeded, and both escalated on
the model's own judgment (submitting either form is state-changing, per its own system prompt)
with no scripting involved. That run also caught a real bug: any capability outside a hardcoded
two-entry table defaulted `risk_level` to `safe` even after needing a human's sign-off — fixed by
inferring `risky` from the run's own transcript instead of a list someone has to remember to
update. Separately verified: fresh discovery starting *directly* on a locked member reasons its
way to `business_outcome`/`PERMISSION_DENIED` cold, no prior capability to guide it. Reviewer
commands: `README.md`, "Teach it something it's never seen."

**A full-codebase review (D29) found this section's own named failure mode, inverted**: both new
capabilities were missing the `expected_outcomes` D14 already established for the other three, so
replaying either against a locked member reported `hard_failure` for a real, expected
`PERMISSION_DENIED` — a business outcome misreported as a break, not the usual direction (a
failure silently reported as success). Same D14 fix, just extended; re-verified live.

## 4. Heterogeneity & multi-tenant

Not built — design only, per scope. The seam that matters is already in place: perception and
`agent/tools.py` are the only Playwright-specific modules; recorder, schema, and replay engine
only ever see `{role, name, value, children}` and role/name-addressed actions. A **legacy web
app** with worse markup needs no changes (this app already is one). A **desktop app** needs a
different perception adapter (OS accessibility APIs — Windows UIA / macOS AX, exposing the same
shape natively) and action executor, but the same `Capability` schema, 4-tier fallback concept,
and replay contract.

**Multi-tenant reuse**: represent a capability as a **base + per-tenant patch** instead of one
artifact per tenant. The base is what most tenants running the same vendor product replay
unmodified. A tenant whose instance differs (rebrand, a renamed field, an extra step) gets a
small patch — step overrides keyed by `step_id`, listing only what differs — applied over the
base at replay time. Drift detection reuses the tier log from section 3: a tier-2/3 spike for
one tenant signals either the base needs updating or that tenant needs its own patch, without
touching the others. A patch is just a partial `Capability` — no new schema needed.

## 5. Escalation & handoff

A file-backed **lease** (`state: automation|human`, `context`) is the entire "who's in control"
model. `trigger_escalation` flips it to `human`, captures a screenshot + reason + URL + run ID
to `/evidence/`, and **blocks** — polling a resume signal — until a separate
`escalation/operator_page.py` Flask process (a real second web server, not an in-process mock)
posts a real HTTP `/resume`. Playwright runs in a **persistent, non-headless-capable** context
specifically so this is the literal same live session a human takes over, not a fresh one.

Triggers: the dead-end detector (3 consecutive turns with an identical accessibility-tree hash,
tested live and offline), a `risk_level: risky` step needing confirmation, or the model
voluntarily calling `escalate` (observed live: given a goal requiring an irreversible submit,
the model escalated on its own, citing the exact policy rule from its system prompt).

**A real gap found by running this live, and closed** (D11→D12): the resume signal originally
carried only a free-text note, so a resumed agent couldn't distinguish "approved" from
"declined." Fixed: `signal_resume` now carries a structured `decision`, threaded back through the
lease's context — what let the risky `open_subaccount` capability get recorded to completion by
one real, unattended run instead of a human sitting and clicking through it live.

**A second, related gap (D30), found the same way — a user actually operating the escalation UI**:
the dead-end path threaded `trigger_escalation`'s return value nowhere, so a human's own note on
resume never reached the model, unlike the `escalate()` path D12 already fixed. Same fix, same
pattern, applied to the branch it had been missed on.

## 6. Safety

`guardrail_check(action, current_url)` — allowlisted domains and action types, loaded once at
startup, checked before every action in **both** discovery and replay. Any violation raises and
halts; there's no silent skip and no in-band bypass. Demonstrated live: an out-of-allowlist
navigation is blocked and the transcript saved to `/evidence/`.

`check_risk_confirmation(risk_level, confirm)` is separate: `risk_level: risky` replay refuses
to execute past confirmation without explicit `confirm=True`, checked before the browser
launches.

`redact(obj)` runs two passes: by **key** (`{ssn, account_number, password, token}`, substring —
not applied to `input_schema`/`output_schema` after `account_number` collided with a
legitimately-named `sub_account_number` field and corrupted its type descriptor, D13), and by
**value shape** (D17: an SSN- or card-number-like digit run is masked regardless of key name —
verified against 1,000 random SHA256 hashes with zero false positives, and deliberately does
*not* flag a name or currency balance, since those belong in a capability's declared outputs).
Full PII detection remains an explicit cut.

The compliance frame that actually applies to a US bank is **GLBA** (the Safeguards Rule), not
HIPAA (healthcare-specific). Three follow-on gaps this raised were closed with real, verified
fixes rather than left as write-up caveats:

- **Discovery must never touch production data** (every turn sends page content to a third-party
  LLM) — now *enforced*: `guardrail_check(phase="discovery"|"replay")` checks a separate,
  stricter `discovery_allowed_domains` list, so a production domain added to the general replay
  allowlist doesn't automatically permit discovery there.
- **The operator console had zero authentication** — the single most safety-critical gap, since
  whoever could reach it could approve an irreversible financial action. Now requires HTTP Basic
  Auth, fail-secure (a random credential is generated and printed if none is configured). Verified
  live: `scripts/smoke_test_operator_auth.py` launches the real subprocess and confirms an
  unauthenticated `/resume` is rejected AND the lease provably stays untouched afterward.
- **Encryption at rest** — `guardrails/encryption.py`, real authenticated encryption
  (`cryptography`'s `Fernet`), keyed from `EVIDENCE_ENCRYPTION_KEY` in `.env` — the same trust
  model already used for `ANTHROPIC_API_KEY`, not a hardcoded key. Verified via
  `scripts/demo_encryption_at_rest.py`: writes a customer-shaped record, confirms nothing (not
  even valid JSON) survives on disk, decrypts back byte-for-byte. Deliberately **not** applied to
  this repo's own `/evidence/`/`capabilities/`, since the assignment requires those stay
  reviewable. Honest limit: one static key, no rotation, no HSM custody — a real KMS is the
  credible next step, not one to build from scratch here.

## 7. Cuts

- **Desktop support and real multi-tenant infrastructure**: design-only (section 4).
- **Operator console UI is intentionally bare** (three plain buttons, no styling) — the scope
  note allows this; *access* to it is what had to be real, and is (section 6).
- **Parameter detection is a fixed `member (\d+)` pattern, exact-match only** — not a general
  slot-filler. Deliberate, and it's exactly what produced a real bug (D13): an earlier
  blind-substring version misattributed a $50 deposit to `member_id` because "50" also appeared
  in the goal text. Now only tags `param_ref` on an exact match to the extracted ID.
- **`redact()`'s value-shape pass covers SSN/card-number shapes only (D17)**, not full PII — that
  needs NLP-grade entity detection, deliberately out of scope.
- **Tier-2 structural locator is simplified** ("first match in DOM order," not a richer
  relative-position description) — real but only exercised via fake match counts in
  `tests/test_recorder.py`, since this app's own role+name pairs are unique by design.
- **Action vocabulary is `click`/`type`/`navigate`/`extract` only** — no drag or file-upload
  primitive. `<select>` dropdowns *are* covered — `type` falls back from `fill()` to
  `select_option()` — but a code-review pass (D28, not a live crash) found this fallback, and
  every other non-timeout Playwright error in `agent/tools.py`, was unreachable: three functions
  caught only `TimeoutError`, not its own base `Error` class, so any other real failure (element
  detached, not visible, ...) propagated uncaught and would have crashed the whole discovery run
  instead of surfacing as a recoverable tool error the model could see. Fixed and verified live
  with a goal needing `open_subaccount`'s non-default account type.
- **Only one stretch goal attempted** (§8) — depth over breadth per the assignment's own
  guidance; time otherwise went into verifying every core requirement's full outcome matrix live
  (the two required capabilities × all 4 replay scenarios each, a live escalation demo, many real
  bugs found and fixed — see `DECISIONS.md`) rather than adding surfaces on top of a
  less-verified core.
- **What I'd build next**: the base+patch tenant model made concrete against a second app
  variant; a real KMS (rotation, envelope encryption, audit-logged key access) in place of
  `EVIDENCE_ENCRYPTION_KEY`'s single static key.

## 8. Stretch goal: agent-facing capability interface

`agent_interface/` exposes every real `capabilities/*.json` as a Claude tool-use catalog
(`catalog.py`) and an invocation surface (`invoke.py`). `Capability.input_schema` is already
`{param: {"type", "description"}}` — exactly a JSON-Schema `properties` object — so the mapping
to a tool is direct, not a translation layer that could drift from what `replay()` accepts. Added
`Capability.description` (a real schema gap: nothing previously carried what a capability *does*,
only its typed I/O), populated from the discovery goal and manually patched onto all 5 existing
artifacts using their own recorded goals — same discipline as every other artifact patch in this
build (D13/D14/D22/D23).

**Safety property, not an afterthought**: `confirm` is a parameter of `invoke_capability()`, never
a field in the tool schema an LLM sees. Exposing it would let a model set `confirm=True` on its
own tool call and silently defeat `check_risk_confirmation`'s existing server-side gate — the same
guardrail already protects `open_subaccount` regardless of who's calling it.

**The first live run found a real bug**: asked "What's the current balance for member 23456?",
Claude had `lookup_member_balance` available with `member_id` clearly declared as a required
parameter — and declined to call it, reading the literal description "member 12345" (the
historical discovery goal, verbatim) as the tool being hardcoded to that one member. A description
written for human provenance review reads like a hardcoded constraint to a model deciding whether
to call a tool. Fixed by reusing existing logic rather than writing new detection:
`_generalize_description` runs the exact regex `agent/recorder.py` already uses to find a
parameterized ID, rewriting "member 12345" → "a member (member_id)" only when `member_id` is
actually declared, so a capability that mentions an ID for an unrelated reason isn't mangled.
Re-ran the same live demo after the fix: Claude correctly called
`lookup_member_balance({"member_id": "23456"})`, the real deterministic replay engine (no LLM in
this path) returned that member's actual balance, and Claude's final answer was correct. Both the
broken-first and fixed-second runs' full transcripts are in `/evidence/`, not quietly discarded.
10 new tests (`tests/test_agent_interface.py`). Full write-up: `DECISIONS.md` D27.
