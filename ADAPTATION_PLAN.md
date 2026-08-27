# Adaptation Plan — MERIDIAN CORE (Demo Day, Fri Aug 28)

Working plan for the Adaptation Project brief (`Adaptation Project — MERIDIAN CORE`). Goal:
point the existing take-home core at the hosted legacy target `web-sample.interface-hiring.com`,
cover its full §2.1 function surface, and wrap it as **API → chatbot → dashboard** — a clean,
correct, demoable whole. Not breadth for its own sake; every piece thin-but-real.

Decision log continues in `DECISIONS.md` at **D36+**. The graded write-up is `ADAPTATION.md`
(≈1–2 pages, 5 sections) — this file is the internal build plan.

---

## 0. Live-target recon (done 2026-08-27, real HTTP against the hosted app)

| Area | Finding | Consequence |
|---|---|---|
| Auth | `POST /signon` (operator, password, `branch` `<select>`) → `MC_SID` cookie → `/menu`. No API. | Core has **no session concept** — new work. |
| Hidden token | `<input type="hidden" name="_token" value="…">`, session-scoped, stable within a session, re-embedded on every review→post form. | **Free** for us: replay clicks the real `Continue`/`Post` buttons; the browser submits hidden fields automatically. Only a problem for raw-HTTP replay, which we don't do. |
| review → post | Two-step forms; step 2 re-embeds all values + token as hidden inputs. | Handled — `open_subaccount` already does review→confirm→post. |
| Numbered menu | Real `<a href="/members?next=transfer">`. | Navigable as-is. |
| Supervisor gate | `teller1` → `hold/review` → HTTP-200 page "SUPERVISOR OVERRIDE REQUIRED". | Place Hold needs a `super1` session — role switching, new work. |
| `?inject=` kinds | `validation`=400, `notfound`=404, `permission`=403, `timeout`=440 (**and clears the cookie**), `maintenance`=503, `server`=500 — each a distinctly-titled HTML page. | Body-text classification works today; HTTP-status is cleaner (small change). |
| Natural errors | Overdraw → HTTP-200 "Insufficient available balance"; bad login → 302 `/signon`. | Business outcomes by body text — fits `_check_expected_outcomes`. |
| **Form-field labelling** | **No `<label for>`, no `aria-label`, no placeholder, no `<th scope=row>`.** Live `aria_snapshot()` shows unlabelled `- textbox` / `- combobox`. `get_by_role("textbox", name="Operator ID")` → **0 matches**. Buttons/links resolve fine. | **The one real blocker.** Tiers 1–2 of the recorder/replay locator model don't bind to most MERIDIAN inputs. Needs a new locator tier. |

---

## 1. Architecture (target end state)

```
 user ──chat──▶  chatbot (thin: Claude + tool catalog)
                    │ HTTP
              Capability API  (Flask — reuse the stack; no new framework)
                GET  /capabilities
                POST /capabilities/{id}/invoke      → Result + run_id
                GET  /runs, GET /runs/{id}
                GET  /runs/{id}/evidence/{name}
                    │ in-process, sync, one global browser lock
              core (contract UNCHANGED)
                agent_interface.invoke_capability → replay/engine.replay()
                  ├─ locator tiers  ◀── +labeled_field, +field_name  (NEW)
                  ├─ session bootstrap  ◀── SessionProvider runs `signon` once  (NEW)
                  ├─ outcome map  ◀── HTTP-status → classification + short body-text  (REPLACES _KNOWN_OUTCOMES)
                  ├─ guardrails.policy   (allowlist / redact / risk — unchanged)
                  ├─ escalation.controller  (operator console :5001 — now on the replay path too)
                  └─ run registry  ◀── evidence/runs.jsonl append-only  (NEW)
 dashboard ──HTTP──┘  (server-rendered, meta-refresh; reads /capabilities + /runs)
```

**Load-bearing decision: the artifact schema does not change.** `Capability` / `Step` /
`LocatorTarget` / `Result` stay byte-compatible. Two additive optional fields only:
`Capability.requires_role` and `LocatorTarget.strategy` gains `"labeled_field"` / `"field_name"`.
Everything MERIDIAN-specific lives in **config** (`allowlist.yaml`, an outcome map) and **one new
locator tier** — so the write-up can say *adapter + config, not rewrite*, and mean it.

---

## 2. Core changes — real vs. config

### 2.1 Config only (minutes)
- `guardrails/allowlist.yaml`: add `web-sample.interface-hiring.com` to `allowed_domains` **and**
  `discovery_allowed_domains`. Add `/settings` to `blocked_routes` for replay — real demo point:
  "the wrapper can't be used to turn off fault injection."
- `CHECKPOINTS` / `RISK_LEVELS` tables in `scripts/run_discovery.py`: add MERIDIAN entries.

### 2.2 New locator tiers — `labeled_field`, `field_name`  *(the blocker fix)*
- `perception.py`: when a control has **no accessible name**, synthesize one from the nearest
  preceding label text (a `.lbl` cell / adjacent `<td>` / sibling text), so the LLM can target it.
- New strategy `labeled_field`: `{strategy, label:"Amount", control:"textbox"}` →
  `xpath=//*[normalize-space()="Amount:" or normalize-space()="Amount"]/following::(input|select|textarea)[1]`,
  scoped to the nearest `<form>`. Recorder emits it when role+name gives 0 matches but the acted
  control sits next to visible label text.
- New strategy `field_name` (last resort, logged like tier-3 text): `css=[name="amount"]`.
  Defensible: on a server-rendered legacy app the field `name` is the actual server contract —
  more stable than visible text, and it is *not* a test-id.
- **Alternatives considered & rejected:**
  - *Screenshot + coordinates (computer-use):* throws away the structural signal we do have;
    MERIDIAN's structure is fine, only its *naming* is missing. Brittle to layout.
  - *Inject `<label>`/aria via JS before perceiving:* mutating the target page is exactly what a
    guardrail-conscious system must not do; fragile.
  - *Full custom accessible-name recompute:* overkill; label-proximity captures ~95% at ~5% of
    the cost.

### 2.3 Session handling  *(new)*
- `signon` is recorded as a first-class capability (it's in §2.1) — **but not concatenated into
  every other artifact.** A `SessionProvider` runs `signon` once, keeps the authenticated
  Playwright context, and hands it to `replay()` for the target capability.
- Credentials from env: `MERIDIAN_OPERATOR` / `MERIDIAN_PASSWORD` / `MERIDIAN_BRANCH` (+
  `MERIDIAN_SUPERVISOR_*`). Never CLI args, never artifacts — same trust model as
  `ANTHROPIC_API_KEY`.
- Session-expiry mid-flow (`440` / redirect to `/signon` / title "Sign On") → classify
  `recoverable` → `recoverable_handled`, stop cleanly (matches the take-home's `recoverable`
  semantics; no silent retry).
- **Alternatives considered & rejected:**
  - *Bake the full sign-on sequence into every capability's steps:* puts credentials in every
    artifact, re-logs-in on every invoke, ignores that sessions are a real thing here.
  - *Shared long-lived global session:* idle-timeout would break unrelated invokes
    unpredictably; per-invoke bootstrap is cheap enough (seconds) and isolates failures.

### 2.4 `requires_role` on `Capability`  *(additive)*
Optional field, default `null`. `place_account_hold` → `requires_role: "supervisor"`. The API's
session bootstrap for such a capability uses `MERIDIAN_SUPERVISOR_*`; if unset → the run
**escalates** ("supervisor credentials required") rather than hard-failing.
- **Why a new field, not reuse `risk_level`:** riskiness (gates `confirm`) and *which credential*
  are orthogonal. Mirrors the take-home's own split of `guardrail_check` vs
  `check_risk_confirmation` (D5). One field, one job.

### 2.5 Outcome classification — replace `_KNOWN_OUTCOMES`
My take-home review flagged `agent/compiler.py::_KNOWN_OUTCOMES` as a static dict keyed by the 5
mock `capability_id`s with mock-app copy — it doesn't generalize. The adaptation forces the fix:

1. **Global HTTP-status map** (`surface/meridian_outcomes.yaml`), applied to *every* capability:
   `400→business VALIDATION_REJECTED`, `403→business PERMISSION_DENIED`,
   `404→business RECORD_NOT_FOUND`, `440→recoverable SESSION_EXPIRED`,
   `503→recoverable MAINTENANCE`, `500→hard_failure SERVER_ERROR`.
2. **Per-capability body-text business outcomes**, short and target-specific: `INSUFFICIENT_FUNDS`
   ("Insufficient available balance"), `SUPERVISOR_OVERRIDE_REQUIRED` ("SUPERVISOR OVERRIDE
   REQUIRED"), `MEMBER_NOT_FOUND` (search returns no rows), `INVALID_CONTACT` (bad email/phone).
3. Replay checks **HTTP status first** (deterministic, copy-independent), then body-text, then
   checkpoint. No match on an error page → `hard_failure` with step/expected/observed/screenshot,
   unchanged.
- Needs: capture the main-frame document HTTP status. `page.goto()` returns a `Response`; for
  click-triggered nav use `page.expect_navigation()` / `page.on("response")`. Store `last_status`
  on the replay context; `_check_expected_outcomes` consults it.

### 2.6 Secrets on the new path
- `redact()` gains: a `Step.value` typed into a control labelled/named like `password` →
  `***REDACTED***`; `MC_SID` / `_token` value shapes added to the structured-secret patterns.
- Credentials only from env; the API never accepts `confirm` or credentials in a request body.

### 2.7 Escalation on the replay/API path  *(new — currently discovery-only)*
`replay/engine.py` today just pre-flight *refuses* a risky capability without `confirm`. On the
API path, a risky capability instead calls `trigger_escalation` → operator console :5001 →
human approves → resume → post. This is "escalation preserved through the new surface" and gives
the demo its "one that escalates" beat.

---

## 3. New surfaces (all deliberately thin)

### 3.1 Capability API — Flask (reuse stack), single process, sync
- `GET /capabilities` → `{id, description, input_schema, output_schema, risk_level, requires_role}`.
- `POST /capabilities/{id}/invoke` `{args, idempotency_key?}` → `Result` + `run_id`. **No `confirm`
  in the body** — risky → escalation, caller polls `GET /runs/{id}`.
- `GET /runs`, `GET /runs/{id}`, `GET /runs/{id}/evidence/{name}`, `GET /health`.
- Run registry: `evidence/runs.jsonl` append-only, one line per run, written by `replay()` and
  `run_discovery.py`. No DB.
- Concurrency: one global lock around `replay()` (it drives one browser); concurrent invokes
  queue. Documented limit — a queue would be the premature infra the brief says not to build.

### 3.2 Chatbot — CLI REPL (certain), optional 40-line web box
Real Claude tool-use over the catalog from `GET /capabilities`; each tool call →
`POST …/invoke`; render `Result` in plain language ("Transferred $1.00 … Confirmation 8837." /
"Couldn't complete: insufficient balance in the source share." / "Needs supervisor approval —
routed to the operator console."). One model call per turn, tools only.

### 3.3 Dashboard — server-rendered Flask blueprint, meta-refresh
- `/` — capability catalog + run-history table (run_id, capability, status badge, started,
  duration), auto-refresh 3s via `<meta http-equiv=refresh>`.
- `/runs/{id}` — inputs, structured outputs, status, tier log, evidence (failure/escalation
  screenshot inline, DOM snapshot link, discovery transcript link, step list).
- Plain style, consistent with the existing bare operator console. No JS framework — the brief
  explicitly does not reward that.

---

## 4. Sequenced delivery

**Phase A — prove the surface — ✅ DONE (D37), verified live**
- A1 ✅ config: `web-sample.interface-hiring.com` in both allowlists; `/settings` blocked both phases.
- A2 ✅ `agent/legacy_locate.py` (`labeled_field` + `field_name`); `perception.py` enriches
  unnamed controls; `recorder.py` / `replay/engine.py` / `tools.py` wired; schema strategies
  added. `scripts/smoke_meridian_signon.py` drives the real login end to end (8/8). +26 tests.
- A3 ✅ `replay()` takes an optional `page=`; `capabilities/meridian_signon.v1.json` (credentials
  as params, none stored); `agent/session.py::run_with_session` (env creds only); compiler scrubs
  literal password values. `scripts/smoke_meridian_session.py`: signon → authed target replay on
  one session (3/3). +6 tests.
- **Gate MET:** `run_with_session` logs in and reaches a member record, by real run.

**Phase B — the two mandated capabilities — ✅ DONE (D39), verified live**
- B1 ✅ `meridian_check_member_balance` — real LLM discovery run (`discovery_run_f9e05c9d33`).
  Replays `success` for member 100987 (its own data) via `table_position`. Forced out 3
  robustness fixes (cell extraction, dead-end-on-reads, `<td>` table headers) + a checkpoint fix.
- B2 ✅ `meridian_funds_transfer` — scripted recorder (documented, like signon). `risk_level:
  risky`, fully parameterised (`{member_id}` URL template + from/to/amount/memo params). Replays
  `success` with `--confirm` (confirmation `CN480425`, real post); `hard_failure` at the risky
  gate without it.
- New: `labeled_value` locator strategy; `_resolve_value` URL templating; perception token cap;
  `scripts/run_meridian.py` session-aware replay CLI.

**Phase C — error taxonomy — ✅ DONE (D40), full matrix verified live**
- C1 ✅ `surface/outcomes.py` + `surface/meridian_outcomes.yaml` — per-TARGET profile (replaces
  the per-capability-id `_KNOWN_OUTCOMES` dict). HTTP-status map + body-text conditions,
  body-first precedence. `replay/engine.py` captures main-frame HTTP status.
- C2 ✅ natural business outcomes: `MEMBER_NOT_FOUND`, `SOURCE_SHARE_ON_HOLD`, `INSUFFICIENT_FUNDS`.
- C3 ✅ every `--inject` kind (400/403/404/440/503/500) on both mandated capabilities +
  natural cases. Evidence: `evidence/replay_c_inj_*`, `replay_t_*`. `scripts/run_meridian.py
  --inject`, `scripts/meridian_inject.py`.

**Phase D — wrappers — ✅ DONE (D41), verified live end to end**
- D1 ✅ `api/app.py` Flask API + `agent_interface/runs.py` (`evidence/runs.jsonl`).
- D2 ✅ `replay(risky_mode="escalate")` → operator console approve→post (`CN480430`) / decline→`escalated`.
- D3 ✅ `chatbot/cli.py` — Claude tool-use over the API.
- D4 ✅ server-rendered dashboard (`/`, `/runs/<id>`), meta-refresh, evidence inline.
- New: `Capability.requires_role` (additive).

**Phase E — remaining §2.1 surface (after the spine is solid)**
- E1 `open_new_share` (risky, review→post).
- E2 `update_member_info` (email/phone/address; natural `INVALID_CONTACT`).
- E3 `place_account_hold` — recorded under `super1`; `requires_role: supervisor`; teller path →
  `SUPERVISOR_OVERRIDE_REQUIRED`; supervisor path → escalate → approve → post.
- E4 `member_inquiry` by last name (the "or by last name" branch).
- E5 `signon` as a standalone catalog entry.

**Phase F — demo hardening**
- F1 reset-safe demo script (exact commands, seed member IDs, which inject to fire).
- F2 screen recording of every path (happy, each error, escalation) — network backup.
- F3 `ADAPTATION.md` (5 sections) + `DECISIONS.md` D36–Dxx.
- F4 fresh-clone check: install, env, `playwright install`, run API + dashboard + chatbot, one
  happy + one error + one escalation.

---

## 5. "Super better if time permits" (bracketed — not core, listed so we can defend the line)

- Full `SurfaceAdapter` ABC extracting every Playwright touchpoint behind one interface — makes
  "desktop app = new adapter" literally true, not aspirational.
- Bounded single-step re-auth on `SESSION_EXPIRED` + resume-from-checkpoint, recorded as evidence
  (never open-ended).
- Canonicalization: `/members/100234/transfer` → `/members/:id/transfer` in the artifact, so one
  recording covers all members with no per-member literals.
- `idempotency_key` dedupe in the registry — a re-asked chatbot request never double-posts.
- Multi-run stability: replay each capability N times, show flake rate on the dashboard.
- `draft → approved` state on a capability; API refuses unattended invoke of a `draft`.
- Assisted fallback: one bounded, policy-checked LLM recovery attempt on a single failed step,
  recorded (matches the take-home stretch list).

---

## 6. Deliberate cuts (state them in the write-up)

- Real KMS for at-rest encryption — carried over from the take-home.
- General slot-filling for parameters — `member_id` / `amount` / share params only; other fields
  are recorded literals or env.
- Auto-relogin recovery — session expiry is **detected and reported**, not silently healed
  (unless the §5 item lands).
- Concurrent replay — one browser, one global lock, invokes queue.
- Dashboard auth — read-only over synthetic data; the operator console (which can approve money
  movement) keeps its Basic Auth.
