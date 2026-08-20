# REPORT

## 1. Architecture

Single Python process, no queues, no services — the assignment explicitly penalizes premature
scaling infrastructure, and nothing here needs it. Six modules, each with one job:

- **`app/`** — the target: a mock legacy core-banking Flask/SQLite app with hostile markup
  (nested tables, non-semantic classes, zero test IDs) but real semantic HTML underneath
  (`<button>`, `<label for>`, `<th scope=row>`), plus a real `<iframe>` boundary for the
  sub-account confirmation step — the stress case for "no clean DOM."
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
per step (`retry_on: transient_load`), not blanket-applied.

All four required scenarios ran against real compiled capabilities, for both capabilities:
success with a never-recorded `member_id`, both business outcomes, and an injected hard failure
(target pointed at a nonexistent route) — real output saved to `/evidence/`.

## 4. Heterogeneity & multi-tenant

Not built — design only, per scope. The seam that matters is already in place: perception and
`agent/tools.py` are the only modules that know about Playwright specifically; recorder, schema,
and replay engine only ever see `{role, name, value, children}` and role/name-addressed actions.
A **legacy web app** with worse markup needs no changes (this app already is one). A **desktop
app** needs a different perception adapter (OS accessibility APIs — Windows UIA / macOS AX,
exposing the same role/name/value shape natively) and a different action executor, but the same
`Capability` schema, 3-tier fallback concept, and replay contract.

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
"declined." Fixed: `signal_resume` now carries a structured `decision`, the operator page has
explicit buttons for each, and `resume()` threads it back through the lease's context — this is
what let the risky `open_subaccount` capability get recorded to completion by one real,
unattended run instead of requiring a human to sit and click through it live.

## 6. Safety

`guardrail_check(action, current_url)` — allowlisted domains and action types, loaded once at
startup, checked before every action in **both** discovery and replay. Any violation raises and
halts; there's no silent skip and no in-band bypass. Demonstrated live: an out-of-allowlist
navigation is blocked and the transcript saved to `/evidence/`.

`check_risk_confirmation(risk_level, confirm)` is separate: `risk_level: risky` replay refuses
to execute past confirmation without explicit `confirm=True`, checked before the browser
launches.

`redact(obj)` runs two passes: by **key** (`{ssn, account_number, password, token}`, substring —
deliberately *not* applied to `input_schema`/`output_schema`, after `account_number` collided
with a legitimately-named `sub_account_number` field and corrupted its type descriptor instead of
protecting real data, D13), and by **value shape** (D17: an SSN or card-number-like digit run is
masked regardless of key name, closing the gap where a secret in an unrelated field would sail
past a key-only check — verified against 1,000 random SHA256 hashes with zero false positives,
and deliberately does *not* flag a name or a currency-formatted balance, since those legitimately
belong in a capability's declared outputs). Full PII detection remains an explicit cut.

The compliance frame that actually applies to a US bank is **GLBA** (the Safeguards Rule), not
HIPAA (healthcare-specific). Three follow-on gaps this raised were closed with real, verified
fixes rather than left as write-up caveats:

- **Discovery must never touch production data** (every turn sends page content to a third-party
  LLM) — now *enforced*, not a convention: `guardrail_check(phase="discovery"|"replay")` checks a
  separate, stricter `discovery_allowed_domains` list. A production domain added to the general
  allowlist for replay (which never calls the LLM) does not automatically permit discovery there.
- **The operator console had zero authentication** — the single most safety-critical gap, since
  whoever could reach it could approve an irreversible financial action. Now requires HTTP Basic
  Auth, fail-secure (a random credential is generated and printed if none is configured). Verified
  live: `scripts/smoke_test_operator_auth.py` launches the real subprocess and confirms an
  unauthenticated `/resume` is rejected AND the lease provably stays untouched afterward.
- **Encryption at rest** — `guardrails/encryption.py`, real authenticated encryption
  (`cryptography`'s `Fernet`; never custom crypto), keyed from `EVIDENCE_ENCRYPTION_KEY` in
  `.env` — the same trust model already used for `ANTHROPIC_API_KEY`/`OPERATOR_PASSWORD`, not a
  hardcoded key. Verified for real via `scripts/demo_encryption_at_rest.py`: writes a
  customer-shaped record to an actual file, confirms nothing (not even valid JSON) survives on
  disk, decrypts it back byte-for-byte. Deliberately **not** applied to this repo's own
  `/evidence/`/`capabilities/`, since the assignment requires those stay reviewable — in a real
  deployment those write paths would call this module. Honest limit: one static key, no rotation,
  no HSM custody — a real KMS is the credible next step, not one to build from scratch here.

## 7. Cuts

- **Desktop support and real multi-tenant infrastructure**: design-only (section 4).
- **Operator console UI is intentionally bare** — the scope note allows this, and it's still true
  (three plain buttons, no styling). What's no longer a cut, as of D18: *access to it* — that now
  requires real authentication, not just a real lease mechanism underneath a bare page.
- **Parameter detection is a fixed `member (\d+)` pattern, exact-match only** — not a general
  slot-filler. Deliberate, and it's exactly what produced a real bug (D13): an earlier
  blind-substring version misattributed a $50 deposit to `member_id` because "50" also appeared
  in the goal text. Now only tags `param_ref` on an exact match to the extracted ID.
- **`redact()`'s value-shape pass covers SSN/card-number shapes only (D17)**, not full PII (a
  name, an address) — that needs NLP-grade entity detection, a much harder, false-positive-prone
  problem, deliberately out of scope.
- **Encryption at rest is built (D19) but not applied to this repo's own `/evidence/`** — the
  capability is real and tested (`guardrails/encryption.py`), deliberately not wired into this
  submission's evidence writes since the assignment requires those to stay reviewable. Remaining
  honest limit: one static key, no rotation, no HSM custody — a real KMS is the credible next step.
- **Tier-2 structural locator is simplified** ("first match in DOM order," not a richer
  relative-position description) — real but only exercised via fake match counts in
  `tests/test_recorder.py`, since this app's own role+name pairs are unique by design.
- **MCP capability-server stretch goal**: not attempted — time went instead into verifying every
  core requirement's full outcome matrix live (both capabilities × all 4 replay scenarios, a live
  escalation demo, several real bugs found and fixed — see `DECISIONS.md`) rather than adding a
  new surface on top of a less-verified core.
- **What I'd build next**: the base+patch tenant model made concrete against a second app
  variant; a richer tier-2 locator description; a real KMS (rotation, envelope encryption,
  audit-logged key access) in place of `EVIDENCE_ENCRYPTION_KEY`'s single static key.
