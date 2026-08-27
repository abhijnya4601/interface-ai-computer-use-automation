# Adaptation write-up — MERIDIAN CORE

The take-home core (discover → typed capability artifact → deterministic replay, with guardrails,
evidence, escalation) pointed at the hosted legacy target `web-sample.interface-hiring.com`, its
full §2.1 surface covered, and wrapped as **API → chatbot → dashboard**. Every decision and the
bugs found along the way are in `DECISIONS.md` (D36–D44); this is the summary.

## 1. What adapting took, and what changed in the core

**The artifact schema did not change.** `Capability` / `Step` / `LocatorTarget` / `Result` stay
byte-compatible; every take-home test still passes. Additions are all optional/additive: three
new `LocatorTarget.strategy` values and `Capability.requires_role`. Everything MERIDIAN-specific
lives in **config** (`guardrails/allowlist.yaml`, `surface/meridian_outcomes.yaml`) and **two new
adapter modules** (`agent/legacy_locate.py`, `agent/session.py`). So the answer to "adapter/config
or rewrite" is: adapter + config.

**The one real coupling.** MERIDIAN's form markup has no `<label for>`, no `aria-label`, no
placeholder, no `<th scope="row">` — a live `aria_snapshot()` shows bare `- textbox` nodes and
`get_by_role("textbox", name="Operator ID")` returns **0**. That is recorder tiers 1–2 and
`replay/engine.py::_locate` failing for nearly the whole target (login, search, transfer, hold,
update). The take-home's own mock app *deliberately* gave itself clean ARIA, so this is exactly
the "too coupled to your original target" signal the brief asks about. The fix is a bounded
adapter, not a rewrite:

- `agent/perception.py` synthesises a name for each unnamed control from the visible label cell
  next to it, so the LLM can target it.
- `agent/legacy_locate.py` adds `labeled_field` (resolve by that visible label, form-scoped
  XPath), `field_name` (`css=[name="…"]` — the real server contract on a legacy app, not a
  test-id; recorded as a fallback tier), and `labeled_value` (read a value from a
  `<td class="lbl">Confirmation:</td><td>…</td>` row by the label).
- Rejected: screenshot+coordinates (discards the structural signal MERIDIAN *does* have),
  injecting `<label>`/aria before perceiving (mutating the target), a full accessible-name
  recompute (overkill).

**Session.** MERIDIAN 302s every route to `/signon` without an `MC_SID` cookie and idle-times-out.
`meridian_signon` is a first-class recorded capability with `operator`/`password`/`branch` as
typed params — **no credential is stored in the artifact** — and `agent/session.run_with_session`
signs on once against a browser context then replays the target capability on that same
authenticated page (`replay()` gained an optional `page=`). Credentials come from the environment
only. Baking login into every capability (credentials in every artifact, re-auth per call) and a
shared long-lived session (idle-timeout breaks unrelated invokes) were both rejected.

**Other core changes**, all small and defended in `DECISIONS.md`: `execute_extract` /
`_extract_value` return a data-table cell's own text (the take-home's row-relative heuristic is
right only for a `<th scope=row>` label); perception collapses a `<select>`'s option list to a hint string and surfaces every control's
current value, and a successful `extract`/`type` no longer trips the dead-end detector — without
these three, a discovery run filling a MERIDIAN form (dropdowns, pre-filled inputs) dead-ends;
`trigger_escalation` takes a bounded `max_wait_s` so an unattended run can't block forever.

## 2. The capability API

One Flask app (`api/app.py`) — the mock app and operator console are already Flask, so no new
framework. Single process, synchronous, one `threading.Lock` around invoke (replay drives one
browser; concurrent invokes queue). No DB, no queue.

```
GET  /api/capabilities                      -> [{name, description, input_schema (JSON-Schema
                                                 properties), risk_level, requires_role,
                                                 needs_session}]
POST /api/capabilities/<id>/invoke  {args, role?}
                                            -> {run_id, result: Result, run}   (409 on a
                                               failure/escalated status, 200 otherwise)
GET  /api/runs  /  /api/runs/<id>  /  /api/runs/<id>/evidence/<name>
GET  /api/health
```

The contract *is* the artifact's `input_schema` / `output_schema` / `Result` — the API is a thin
transport, not a translation layer. **`confirm` is never a request field** (an LLM could set it):
a risky capability runs to its final click, then routes an intervention request to the operator
console. Session-gated MERIDIAN capabilities go through `run_with_session`; others through
`invoke_capability`. `meridian_signon` is excluded from the catalog — it's a precondition, not a
task. Every invoke writes a redacted line to `evidence/runs.jsonl`, which the dashboard
(server-rendered, `<meta refresh>`) and `GET /api/runs` read.

The chatbot (`chatbot/cli.py`) is a REPL: one Claude tool-use call over the catalog, one API
invoke, one call to phrase the `Result` / business outcome / escalation.

## 3. Driving the legacy UI, and its runtime/exceptional states

**Reliable driving.** Replay clicks the real `Continue` / `Post` buttons through Playwright, so
MERIDIAN's per-transaction hidden `_token` is submitted by the browser automatically — a point
where "drive the real browser, don't reconstruct requests" pays off. Locators resolve through the
tier ladder: `role_name` → `structural` → `labeled_field` → `field_name` → `labeled_value` →
`table_position` → `text`. Data-table values (the SHARES/BALANCES table) are addressed by
**position** (which table by its column headers, which row, which column), never by the value —
verified: replaying `check_member_balance` against member 100987 returns *its* `$56.00` row, not
the recorded member's.

**Error taxonomy** (`surface/outcomes.py` + `surface/meridian_outcomes.yaml`) — this replaces the
take-home's per-`capability_id` `_KNOWN_OUTCOMES` dict (which didn't generalise) with a
per-**target** profile applied to every step of every capability. Precedence: a step's own
declared outcomes → **body-text conditions** → **HTTP-status map**. Body before status is
deliberate — MERIDIAN returns 400 for both "source share is HOLD" and "insufficient balance", and
the caller needs those distinguished. `replay()` captures the main-frame document HTTP status
(`page.goto` return value + a `page.on("response")` listener).

Verified live, full matrix (evidence in `evidence/replay_c_inj_*`, `replay_t_*`, `replay_e_*`):

| condition | HTTP | class | code |
|---|---|---|---|
| `?inject=notfound` / bad member | 404 | business_outcome | `RECORD_NOT_FOUND` |
| `?inject=validation` | 400 | business_outcome | `VALIDATION_REJECTED` |
| `?inject=permission` | 403 | business_outcome | `PERMISSION_DENIED` |
| `?inject=timeout` | 440 | **recoverable_handled** | `SESSION_EXPIRED` |
| `?inject=maintenance` | 503 | **recoverable_handled** | `MAINTENANCE` |
| `?inject=server` | 500 | **hard_failure** | `SERVER_ERROR` |
| search miss (natural) | 200 | business_outcome | `MEMBER_NOT_FOUND` |
| HOLD source share (natural) | 400 | business_outcome | `SOURCE_SHARE_ON_HOLD` |
| overdraw (natural) | 400 | business_outcome | `INSUFFICIENT_FUNDS` |
| bad email on update (natural) | 400 | business_outcome | `INVALID_CONTACT` |
| teller attempts Place Hold | 403 | business_outcome | `PERMISSION_DENIED` |

**Recovery is bounded and declared, never open-ended.** A recoverable outcome in
`surface/meridian_outcomes.yaml` carries a `recovery` hint: `503 MAINTENANCE` →
`{action: retry, max_attempts: 3, backoff_ms: 700}`, `440 SESSION_EXPIRED` →
`{action: reauth_and_retry, max_attempts: 1}` (re-runs the `meridian_signon` capability on the
same page, then retries the step). `replay()` performs exactly that — re-executes the failed
step up to N times, re-checking after each. If it clears, the run **continues** and can still end
`success`; the `Result.recovery` list records `{step_id, code, action, attempts, outcome}`. If it
doesn't clear (a *forced* `?inject=` fault never will), replay stops at `recoverable_handled`
with the same log — the caller gets a status distinct from `hard_failure` meaning "I tried to
recover, couldn't, safe to retry the whole run later". Verified live both ways: forced
`--inject maintenance` → retry ×3 → `gave_up`; a transient error-rate 503/440 mid-transfer →
`recovered`, run continues.

## 4. Safety, evidence, escalation through the new surface

- **Allowlist** — `web-sample.interface-hiring.com` added to both `allowed_domains` and
  `discovery_allowed_domains`; `/settings` (fault-injection screen) is in `blocked_routes` for
  **both** phases, so the wrapper can't be used to disable the fault handling being demonstrated.
  `guardrail_check` already runs inside `replay()` per step; the API inherits it.
- **Secrets** — credentials only from the environment, never a request body, never an artifact
  (`meridian_signon`'s three credential fields are `param_ref`s). `save_capability` also scrubs a
  literal value typed into a password-looking control. `runs.jsonl` params/outputs go through
  `redact()`.
- **Risky / irreversible** — `confirm` is not exposed to any caller. On the API path a risky
  capability (`meridian_funds_transfer`, `open_share`, `place_hold`) pauses before its final
  click and routes an intervention request — capability, current step, reason, current URL, and a
  screenshot — to the operator console on :5001 (HTTP Basic Auth). Approve → the step runs;
  decline → `status: escalated`, nothing committed. Verified end to end:
  `api_1787819357562` (approve → posted, `CN480430`), `api_1787819397349` (decline → escalated).
- **Supervisor override** — `Capability.requires_role` (orthogonal to `risk_level`). Place Hold
  is `requires_role: supervisor`; the session signs on with `MERIDIAN_SUPERVISOR_*`. As a teller
  it returns `business_outcome / PERMISSION_DENIED`; with no supervisor credentials configured it
  returns `status: escalated` (a human supplies them), not a crash.
- **Evidence** — every run: a `runs.jsonl` line, the take-home's discovery transcript / failure
  screenshot / escalation screenshot+context, and the locator tier log (also the drift signal).

## 5. Cuts, and what's next

**Cut, deliberately:**
- **A small `generalize()` pass after discovery.** All 7 §2.1 capabilities come from a real
  LLM discovery run (`scripts/discover_all_meridian.py`), but a freshly-discovered capability
  has a concrete member id in its entry URL and concrete values in its form steps. A
  `surface/meridian_flows.py::generalize()` pass rewrites the URL to a `{member_id}` template,
  maps each recorded literal to a typed param, and sets the risk level / required role /
  checkpoint. This is a deliberate seam, not per-run hand-editing: the same spec would drive a
  discovery-time parameter-naming step in a fuller system.
- **Concurrency** — one browser, one lock; invokes queue.
- **Dashboard auth** — read-only over synthetic data; the operator console keeps its auth.
- **Real KMS** for at-rest encryption — carried over from the take-home.

**Next, with more time:** a full `SurfaceAdapter` ABC extracting every Playwright touchpoint
behind one interface (makes "desktop = new adapter" literally true); single-step re-auth +
resume-from-checkpoint on `SESSION_EXPIRED`, recorded as evidence; route canonicalisation
(`/members/:id/…`) so one recording covers all members with no per-member literals; an
`idempotency_key` on invoke so a re-asked chatbot request never double-posts; a multi-run
stability signal on the dashboard.
