# REPORT

## 1. Architecture

Single Python process, no queues, no services — the assignment explicitly penalizes premature
scaling infrastructure, and nothing here needs it. Six modules, each with one job:

- **`app/`** — the target: a mock legacy core-banking Flask/SQLite app with deliberately hostile
  markup (nested tables, non-semantic classes, zero test IDs) but real semantic HTML underneath
  (`<button>`, `<label for>`, `<th scope=row>`), plus a real `<iframe>` boundary for the
  sub-account confirmation step — the stress case for "no clean DOM."
- **`agent/perception.py`** — turns whatever Playwright can see into a small
  `{role, name, value, children}` tree, merging content from inside iframes so the LLM never has
  to reason about frame boundaries.
- **`agent/{tools,discovery,recorder}.py`** — the live, LLM-driven observe→decide→act loop and
  the code that turns each accepted action into a typed `Step` alongside it, not as a later pass
  over a transcript.
- **`agent/compiler.py`** + **`artifact/schema.py`** — turns a run's recorded steps into a
  versioned `Capability`, the artifact contract everything downstream depends on.
- **`replay/engine.py`** — the deterministic, no-LLM production execution path.
- **`guardrails/`** and **`escalation/`** — cross-cutting: allowlist enforcement and redaction
  wired into both discovery and replay; a file-backed lease for human handoff.

**Key trade-off — perception API.** The build brief specified `page.accessibility.snapshot()`.
That API no longer exists in current Playwright (verified directly: `AttributeError: 'Page'
object has no attribute 'accessibility'`). Rebuilt perception on `Locator.aria_snapshot()`
(YAML text) instead, parsed into the same tree shape, with per-frame snapshotting to cross
iframe boundaries — also verified directly that a top-level snapshot does *not* reach iframe
content by default, and that reading inside a frame and clicking inside a frame are two
different APIs that both had to be checked, not assumed. Full story in `DECISIONS.md` D6.

**Key trade-off — model.** `claude-sonnet-5`, not the largest available model. This is a
production capability-discovery agent for a bank: cost and latency matter more than squeezing
out marginal reasoning quality on a task this constrained (a handful of tool calls against a
small action space), and the model needs to reliably follow one explicit safety rule (escalate
before irreversible actions) more than it needs frontier reasoning.

## 2. Artifact schema

`artifact/schema.py` — the deliberately most heavily-invested piece. A `Capability` is:
`capability_id`, semver `version`, `created_from_run_id` (provenance), `target` (app/entry
point/surface type), `risk_level` (`safe`/`risky`), typed `input_schema`/`output_schema`, a
`checkpoint`, and `steps: list[Step]`.

Each `Step` carries a `LocatorTarget` (`strategy`, `primary`, `fallbacks`, and a required
`reasoning` string — a locator with no stated reasoning is exactly the kind of unreviewable
artifact this schema exists to prevent), a `value` that's either a literal or
`{"param_ref": "member_id"}`, a `wait_policy`, and — the load-bearing field —
`expected_outcomes: list[ExpectedOutcome]`, each one `{condition, classification,
code, handling}` with `classification` one of `business_outcome` / `recoverable` /
`hard_failure`. These are declared onto the artifact by `agent/compiler.py` from domain
knowledge of the target app (a single discovery run only ever observes its own happy path;
someone finalizing a capability for production has to document the branches they know exist —
same spirit as `reasoning`), and replay evaluates them as **literal, deterministic checks**
against the live page — never a guess, never an LLM call.

This exact design point produced the most interesting bug in this build (D14): a declared
`PERMISSION_DENIED` outcome was attached to the wrong step because it was reasoned from the
server-side route logic instead of what the UI actually renders. The fix wasn't "trust the
schema less" — it was "replay every declared branch against the real app," which the schema's
shape makes checkable in the first place. A vaguer schema (e.g. a single unstructured
`on_error` field) wouldn't have surfaced *which step* was wrong this clearly.

## 3. Determinism & error handling

Replay (`replay/engine.py`) resolves every `Step.target` the same way the recorder declared it:
tier 1 `role_name` (unique role+accessible-name match, backed by real semantic HTML, not
CSS/IDs), tier 2 `structural` (declared `nth` when role+name isn't unique), tier 3 `text` (raw
text-content match, logged as a warning — most brittle). This project's own app never needed
tier 2/3 in practice (every role+name pair is unique by design), but both are real, exercised
code paths (`tests/test_recorder.py` proves tier selection with fake match counts, independent
of whether this specific app ever triggers them).

**The tier log doubles as a free drift-detection signal**: if a capability starts needing tier
2/3 more often across successive replays, the underlying UI has drifted — at zero extra
infrastructure cost, since the log is already being written for every replay.

Each step's `expected_outcomes` are checked deterministically: when the declared locator can't
be resolved (or fails to act), and after a successful action, replay checks whether any declared
`condition` (a literal `"page contains '<substring>'"`) matches the live page. A match returns
`business_outcome` (a real answer, not a crash — e.g. `MEMBER_NOT_FOUND`) or `recoverable`
(handled and continue); no match on a failure is a `hard_failure`, returned with `step_id`,
`expected`, `observed`, and a screenshot reference. `wait_policy` retries are opt-in per step
(`retry_on: transient_load`), not blanket-applied.

All four required scenarios were run against real compiled capabilities, for both capabilities:
success with a never-recorded `member_id` (genuine parameterization, not replaying literal
values), both business outcomes, and an injected hard failure (target pointed at a nonexistent
route) — real output saved to `/evidence/`.

## 4. Heterogeneity & multi-tenant

Not built — design only, per the assignment's scope. The seam that matters is already in place:
perception (`build_observation`) and action execution (`agent/tools.py`) are the only two places
that know about Playwright specifically. Everything downstream — the recorder, the schema, the
replay engine — only ever sees `{role, name, value, children}` and role/name-addressed actions.
A **legacy web app** with worse markup needs no changes at all (this app already is one). A
**desktop app** would need a different perception adapter (OS accessibility APIs — Windows UIA /
macOS AX, which expose the same role/name/value shape natively) and a different action executor
(OS-level click/type instead of Playwright), but the same `LocatorTarget`/`Step`/`Capability`
schema, the same 3-tier fallback concept (native accessibility role+name → structural position →
OCR/text as a last resort), and the same replay engine contract.

**Multi-tenant reuse**: represent a capability as a **base + per-tenant patch** rather than one
artifact per tenant. The base artifact (this build's output) is what most tenants running the
same vendor product replay unmodified. A tenant whose instance differs (rebranded copy, a
renamed field, an extra confirmation step) gets a small patch document — a list of step
overrides keyed by `step_id`, only listing what differs (e.g. `{"step_id": "s4", "target":
{...}}`) — applied over the base at replay time. Drift detection reuses the tier log from
section 3: if tier-2/3 usage spikes for a specific tenant, that's a signal either the base needs
updating or that tenant needs its own patch, without touching the other hundreds of tenants
running the same base capability. This keeps the artifact shape identical to what's already
built — a patch is just a partial `Capability`, no new schema needed.

## 5. Escalation & handoff

A file-backed **lease** (`state: automation|human`, `context`) is the entire "who's in control"
model. `escalation/controller.py::trigger_escalation` flips it to `human`, captures a screenshot
+ reason + current URL + run ID, writes both to `/evidence/`, and **blocks** — polling a resume
signal — until a separate `escalation/operator_page.py` Flask process (a real, second web
server, not mocked as an in-process function call) posts a real HTTP `/resume`. Playwright is
launched with a **persistent, non-headless-capable** context specifically so this is the literal
same live session a human takes over, not a fresh one standing in for it.

Triggers: the discovery loop's dead-end detector (3 consecutive turns with an identical
accessibility-tree hash — genuinely tested, both via a live LLM run and offline), a
`risk_level: risky` step needing confirmation, or the model voluntarily calling `escalate`
(observed live: given a goal requiring an irreversible submit, the model correctly escalated on
its own, citing the exact policy rule from its system prompt, without any dead-end forcing it).

**A real gap found by running this live, and closed** (`DECISIONS.md` D11→D12): the resume
signal originally carried only a free-text note, so a resumed agent had no way to distinguish
"approved, proceed" from "declined, don't." Fixed: `signal_resume` now carries a structured
`decision` (`approved`/`declined`/`None`), the operator page has explicit buttons for each, and
`resume()` threads the decision back through the lease's context — this is what let the risky
`open_subaccount` capability actually get recorded to completion by one real, unattended run
(`--auto-approve-escalation`) instead of requiring a human to sit and click through it live.

## 6. Safety

`guardrails/policy.py`: `guardrail_check(action, current_url)` — an allowlist of domains and
action types, loaded once at startup, checked before every action in **both** discovery and
replay. Any violation raises `GuardrailViolation` and halts; there's no silent skip, and no
in-band way for the model to bypass it (it's checked outside the tool-execution try/except, not
inside a catchable path). Demonstrated live: a deliberately out-of-allowlist navigation is
blocked and the transcript saved to `/evidence/`.

`check_risk_confirmation(risk_level, confirm)` is a separate, cheap check specifically for
`risk_level: risky` capabilities — replay refuses to execute past the confirmation point without
an explicit `confirm=True`, checked before the browser even launches.

`redact(obj)` masks any dict key matching `{ssn, account_number, password, token}`
(case-insensitive substring), recursively, applied to discovery transcripts, guardrail-violation
evidence, and (as of D13) the `steps` portion of a compiled capability — deliberately *not*
`input_schema`/`output_schema`, after the substring match on `account_number` collided with a
legitimately-named `sub_account_number` field and corrupted its type descriptor rather than
protecting any real data (there was none there to protect). **Limit**: matching is by key name,
not by scanning string values for secret-shaped content — a secret embedded in free text
wouldn't be caught. Documented as a cut below, not silently limited.

## 7. Cuts

- **Desktop support and real multi-tenant infrastructure**: design-only (section 4), not built —
  matches the assignment's explicit "design, not necessarily build."
- **Operator console UI is intentionally bare** — three buttons and a screenshot, no real-time
  co-browsing. The scope note explicitly allows this; what's real underneath is the lease flip
  and the persistent session.
- **Parameter detection is a fixed `member (\d+)` pattern, exact-match only** — not a general
  NLP slot-filler. This is deliberate (see `agent/recorder.py`'s docstring) and it's exactly what
  produced a real bug (D13): an earlier, blinder substring-match version misattributed a $50
  deposit amount to the `member_id` parameter because "50" happened to also appear in the goal
  text. The current version only tags a value as `param_ref` if it exactly equals the goal's
  extracted member ID.
- **`redact()` matches by key, not by value content** — won't catch a secret embedded in a
  sentence. A production version would want a secondary pass scanning for secret-shaped
  substrings (SSN/card-number patterns) independent of key names.
- **Tier-2 structural locator is simplified** — "first match in DOM order" rather than a richer
  relative-position description (e.g. "2nd row of results table"). This project's app never
  exercises tier 2 in practice (every role+name pair is unique by design), so it's real but
  untested against this specific app; `tests/test_recorder.py` exercises it directly with fake
  match counts instead.
- **MCP capability-server stretch goal**: not attempted — time was spent instead on verifying
  every core requirement's full outcome matrix live (both capabilities × all 4 replay scenarios,
  a live escalation demo, three real bugs found and fixed) rather than adding a new surface on
  top of a less-verified core.
- **What I'd build next**: the base+patch tenant model from section 4, made concrete against a
  second variant of the mock app; a richer tier-2 locator description; a secondary
  value-scanning redaction pass.
