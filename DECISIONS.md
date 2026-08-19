# Decisions log

Running log of every non-obvious choice made while building this project, in the order made.
Never edit old entries away — append corrections as new entries instead.

## D0 — 2026-08-19 — Starting from a genuinely empty directory

The working directory (`/Users/abhijnyamenakur/Desktop/Interface_Bank_AI`) was empty when this
build started — not a git repo, no files. An earlier `HANDOFF.md` (provided alongside the
assignment PDF) describes Phases 0/1(offline)/schema/guardrails as already built and verified in
a prior sandbox session, but none of that work is present here. Rather than trust the handoff's
claims, this build starts from scratch and re-verifies every phase itself, per the handoff's own
"verify with real output, don't presume" discipline. The handoff and build-spec PDFs are still
used as the source of decisions-already-made (tech stack, schema shape, phase order); the
assignment PDF (`Assignment A — Computer-Use Automation System.pdf`) is the authoritative spec
for *what's graded* whenever the two disagree.

Environment check at start: Python 3.14.5 (spec asks 3.11+, fine), pip, node, git, gh CLI all
present; network reachable. `ANTHROPIC_API_KEY` is NOT set — this blocks Phase 2 (the
non-negotiable real LLM-driven discovery run) but nothing before it, so Phases 0/1/schema/
guardrails proceed first and the key is requested only when actually needed.

## D1 — 2026-08-19 — git initialized locally now, public push deferred

`git init` run immediately so work is checkpointed in commits as phases complete. Pushing to a
*public* GitHub repo (required for submission) is a visible, hard-to-reverse action — deferred
until the user explicitly confirms, per the assignment's "public GitHub repo" submission
requirement and this environment's policy on visible/external actions.

## D2 — 2026-08-19 — Flask app moved from port 5000 to 5050

macOS's built-in AirPlay Receiver listens on port 5000 by default and answers with
`403 Forbidden` / `Server: AirTunes` before the request ever reaches Flask — confirmed via
`curl -v` showing an AirTunes server header instead of our app. The build-spec's port 5000 is
used consistently everywhere (curl checks, allowlist domain, discovery `--target` default), so
rather than have every developer on macOS hit this silently, the mock app now runs on **5050**
and every reference to `localhost:5000` in this repo (allowlist, scripts, README, evidence) uses
5050 instead. Purely a port-number substitution — no behavioral difference.

## D3 — 2026-08-19 — Phase 0 (mock app) built and verified

Recreated `app/models.py`, `app/app.py`, and all 7 templates per the build-spec (search,
member_detail, new_subaccount, confirm_wrapper, confirm_subaccount, subaccount_success,
error_permission). Templates are hand-authored (the spec's tarball of literal HTML wasn't
available in this environment) but hold the three required properties: no `data-testid`/id
attributes added for automation convenience, `<table>` layout for search results and the member
detail fields, and the sub-account confirmation served from a separate route
(`/member/<id>/confirm-frame`) embedded via a real `<iframe>` in `confirm_wrapper.html` — not
inlined.

Label/value pairs (member detail, confirm-frame) use `<th scope="row">Label</th><td>Value</td>`
— real semantic HTML per the spec's hint — specifically so the recorder's locator strategy can
resolve "the value cell next to the row-header named X" as a *structural* (tier-2) locator
without needing any ID/testid shortcut. `data-role="balance"` on the balance cell exists **only**
to satisfy the assignment's own literal `grep 'data-role="balance"'` verification command from
the build spec — it is not used by the discovery agent or replay engine, which only ever see the
accessibility tree (role/name/value), never raw HTML attributes.

Ran the full curl verification suite against the real running Flask app (port 5050, per D2) —
all 6 required checks plus 3 extra sanity checks (locked member blocked from new-subaccount form,
confirm-frame reachable directly, not-found blocked from new-subaccount form) passed with real
output (captured above/in shell history). Phase 0 is done.

## D4 — 2026-08-19 — Artifact schema (Pydantic) built and verified

`artifact/schema.py` implements `TargetSpec`, `LocatorTarget`, `ExpectedOutcome`, `WaitPolicy`,
`Step`, `Checkpoint`, `Capability`, `Result` per the build spec. One addition beyond the spec's
literal listing: `WaitPolicy` is pulled out as its own `BaseModel` instead of `Step.wait_policy`
staying a bare `dict` — the spec's `Step` snippet types it as `dict = {"timeout_ms": ...}` but a
typed model gets free validation (e.g. `retry_count` must be an int) and is exactly as easy to
serialize, so there's no reason to leave it untyped when literally everything else in this
schema is a Pydantic model on purpose. `LocatorTarget.reasoning` and `Capability.risk_level` are
both required (non-optional) fields — a locator with no stated reasoning or a capability with no
declared risk level is exactly the kind of unreviewable artifact this schema exists to prevent.

`tests/test_schema.py` — 10/10 pass, covering: JSON round-trip via
`model_dump_json`/`model_validate_json`, `Step.value` accepting both a literal and a `param_ref`
dict, `LocatorTarget` rejecting a missing `reasoning`, `ExpectedOutcome`/`Capability.risk_level`/
`Result.status` rejecting values outside their `Literal` sets, and `WaitPolicy` defaults.

## D5 — 2026-08-19 — Guardrails (Phase 6, pulled forward) built and verified

`guardrails/allowlist.yaml` + `guardrails/policy.py` (`guardrail_check`, `check_risk_confirmation`,
`redact`). Two decisions beyond the literal spec text:

- `guardrail_check(action, page)` in the spec becomes `guardrail_check(action, current_url=None)`
  here — the function only ever needs the current URL (for click/type/extract, which act on
  whatever's loaded) or an explicit target URL (for navigate), never the live Playwright `page`
  object itself. Passing the whole `page` would couple this pure policy function to Playwright
  for no benefit; callers in `discovery.py`/`replay/engine.py` pass `page.url` at the call site.
- The spec's "risk_level: risky requires confirmation" guardrail is split into its own
  `check_risk_confirmation(risk_level, confirm)` rather than folded into `guardrail_check` —
  they check different things (where an action is allowed to go, vs. how consequential a whole
  *capability* is) and have different call sites (per-action vs. once per replay invocation), so
  keeping them separate keeps each function's job legible.

The allowlist is loaded once at import time (module-level `ALLOWLIST` global), matching "loaded
once at startup" — the assignment's environment is stable, slow-changing enterprise UIs, so a
live-reloading policy store would be exactly the premature infrastructure the assignment
penalizes.

`redact()` masks by *key* (case-insensitive substring match against `ssn`, `account_number`,
`password`, `token`), recursively through nested dicts/lists, and never mutates its input.
Deliberately does not scan string *values* for secret-shaped content (e.g. a 9-digit number
embedded in a sentence) — that's a much harder, false-positive-prone problem and out of scope;
documented as a cut in REPORT.md.

`tests/test_guardrails.py` — 12/12 pass. Additionally ran `scripts/demo_guardrail_violation.py`
— a real (non-pytest) attempt to navigate outside the allowlist — and confirmed
`GuardrailViolation` was actually raised and the action halted; the transcript (exception type,
message, full traceback) is saved to `evidence/phase6_guardrail_violation.json`. This satisfies
the spec's "demonstrate this with an actual failing case saved to /evidence/, not just asserted
in a unit test" requirement.

## D6 — 2026-08-19 — `page.accessibility.snapshot()` does not exist; redesigned perception around `aria_snapshot()`, with a real, verified fallback for iframe content

The build spec (and HANDOFF.md) specify `page.accessibility.snapshot()` as the primary
perception mechanism. With a real Chromium installed (`playwright install chromium`, no root
needed on macOS — the earlier sandbox's blocker was Linux-only `--with-deps`) and
`playwright==1.62.0` (latest at build time; spec only pinned `>=1.42`), this was tested directly:

```
AttributeError: 'Page' object has no attribute 'accessibility'
```

Playwright deprecated and then fully removed the legacy `Accessibility` class; `dir(page)` on
1.62 confirms there is no `accessibility` attribute at all, only `aria_snapshot` (a `Locator`
method). This is a genuine API-surface change since the build spec was written, not a
misunderstanding — verified with real output, not assumed.

**New design:** `Locator.aria_snapshot()` returns a YAML-formatted string (Playwright's newer,
supported a11y-tree representation) rooted at the locator. `agent/perception.py` calls
`page.locator("html").aria_snapshot()` and parses the YAML text into the same
`{role, name, value, children}` dict shape the rest of the system (schema, tests, recorder)
expects — the downstream contract from the build spec is preserved even though the upstream
Playwright call changed. Verified by hand against real output that the YAML grammar
(`- role "accessible name" [attrs]:` with indentation for children, `: "value"` for form
fields, bare `- role "name"` for leaves) parses cleanly through `yaml.safe_load` first, then a
small regex extracts `(role, name)` from each resulting key/string — this was checked against
real snapshot output for the search page, the results table, and a form field before being
trusted, not assumed to work.

**The iframe question — also tested directly, not assumed:** the HANDOFF explicitly flagged
"verify this claim, don't assume it" for whether the top-level snapshot reaches iframe content.
It does not. Real output, `page.locator("html").aria_snapshot()` on the confirm_wrapper page:

```
- document:
  - heading "Review and Confirm" [level=1]
  - paragraph: Please review the details below...
  - iframe
```

`iframe` appears as a childless leaf — confirmed by then checking `'Confirm and Open Account' in
snap` → `False`. But `child_frame.locator("html").aria_snapshot()`, called on the `Frame` object
for the iframe's own document (obtained from `page.frames`), *does* reach it — confirmed
`'Confirm and Open Account' in frame_snap` → `True`, with the real button visible in the output.
So `build_observation` now: snapshots the main frame, walks the parsed tree for nodes with
`role == "iframe"`, and for each one (matched in document order against `page.frames[1:]`,
i.e. every frame besides the main one) snapshots that child frame separately and grafts its
parsed tree in as `children` on a synthetic node with `role: "Iframe"` (capitalized, to signal
"this is perception-layer synthesis stitching two real AX trees together, not a literal ARIA
role" — a real ARIA role is never capitalized). This is exactly the "synthetic Iframe node"
shape HANDOFF.md anticipated, now backed by a verified real implementation instead of a plan.

**Action-side confirmation, also tested for real:** `page.frame_locator("iframe").get_by_role(
"button", name="Confirm and Open Account").click()` — this is a *different* Playwright API
(`FrameLocator`, for driving inside a frame) from `aria_snapshot`'s per-`Frame` snapshotting
(perceiving inside a frame). Both had to be verified independently per HANDOFF's warning not to
assume one implies the other. Confirmed: the click resolves through the iframe, the form's
`target="_parent"` causes a real top-level navigation to `/new-subaccount/submit`, and
`msg-success` appears in the resulting page — the full cross-frame interaction path works
end-to-end with real Chromium and the real Flask app running.

Net effect: the *intent* of the build spec (accessibility-tree-first perception, not
screenshot+coordinates; iframe content must be reachable) is fully preserved. The concrete API
calls changed because the library moved out from under the spec, and both the change and its
replacement were verified against a live browser before being relied on, per the "verify the
sensor before building the brain" discipline HANDOFF.md itself asks for.

## D7 — 2026-08-19 — Phase 1 complete: offline tests + live check both pass

`agent/perception.py` implements `_parse_aria_snapshot`, `prune_accessibility_tree`, and
`build_observation` per D6's design. `tests/fixtures/accessibility_trees.py` uses *real captured*
`aria_snapshot()` output (not hand-guessed) for the search page, a 2-row results table, and the
confirm-frame content, plus one fixture proving the top-level wrapper page does NOT reach the
iframe's button (documents the exact limitation `build_observation` works around).
`tests/test_perception.py` — 12/12 pass, fully offline.

Then ran `scripts/verify_perception_live.py` for real (Chromium + the real Flask app on 5050,
both actually running): navigated `/search`, confirmed `{role: "button", name: "Go"}` present;
submitted a query, confirmed table/row/cell roles present; drove through the sub-account form to
the iframe confirmation screen, confirmed a synthetic `{role: "Iframe", ...}` node is present
AND `{role: "button", name: "Confirm and Open Account"}` is reachable through it. All 4 checks
PASS with real output (captured above). Phase 1 is done — this is the first genuinely
browser-verified phase in this build, not just unit-tested logic.

## D8 — 2026-08-19 — Phases 2-4 built (tools, discovery loop, recorder, compiler); two real
bugs found and fixed by a pre-flight smoke test before spending API credits

Built `agent/tools.py` (the 6 tools + Playwright execution, role/name resolution across frames),
`agent/discovery.py` (the observe→decide→act loop, all 3 stopping conditions, escalation wiring),
`agent/recorder.py` (3-tier locator builder + param-ref detection, 9/9 offline tests using fake
Playwright-shaped stand-ins), and `agent/compiler.py` (Capability assembly + declared business
outcomes, 5/5 offline tests).

Before spending real Anthropic API credits on the required discovery run, wrote
`scripts/smoke_test_discovery.py` — a scripted-fake-LLM-client harness that drives the *real*
loop mechanics (real Chromium, real Flask app, real guardrail_check, real Recorder) through the
exact lookup_member_balance action sequence, so the loop's plumbing could be verified without
needing Claude to cooperate. This caught two real bugs on the first run:

1. `guardrail_check` was being applied to the `finish` tool call and rejecting it —
   `action type 'finish' is not in allowed_actions [...]`. `finish`/`escalate` are loop-control
   signals (no URL, no page interaction), not page actions, so they're now exempt from the
   page-action allowlist check in `discovery.py`. (They still can't let the *agent* act outside
   the allowlist — every actual click/type/navigate/extract that led up to them was already
   checked.)
2. The recorder was building each step's locator *after* calling the executor, so for a click
   that navigates the page (e.g. clicking "View"), `build_locator` counted role="link"
   name="View" matches on the page it had just navigated *to* (the member detail page, which
   has zero "View" links) instead of the page it acted *on* — it fell back to a spurious tier-3
   text locator every time. Fixed by recording (building the locator) before executing, on every
   action branch, with the freshly-built Step popped back off `recorder.steps` if execution then
   fails, so a failed action never gets baked into the compiled artifact.

After both fixes, the smoke test passes cleanly: 5 steps recorded (1 auto-navigate + 4 scripted
actions), the typed member_id correctly tagged as `{"param_ref": "member_id"}`, every locator
resolved at tier 1 (role_name) — no tier-3 fallback warnings — and the transcript captured both
`llm_response` and `tool_call` entries. This is exactly the kind of bug a from-scratch build
should catch *before* the one non-negotiable real run, not during it.

## D9 — 2026-08-19 — Phase 5 (replay engine) built and verified against the real app

`replay/engine.py` implements `replay(capability, params, confirm=False, headless=True) ->
Result` per the contract: no LLM anywhere in it, 3-tier locator resolution (mirrors the
recorder's tiers, using the declared `nth` for structural targets and the declared `fallbacks`
before falling back further to a bare text match), `wait_policy` retries only for steps tagged
`retry_on: transient_load`, capability-level checkpoint verification, and every declared
`expected_outcomes` condition evaluated as a literal substring check against `page.content()` —
never guessed, never LLM-judged. `replay()` owns its own Playwright browser lifecycle (launch
per call) rather than requiring a pre-existing page, so it's a plausible thing an AI agent calls
directly as a production tool.

One deliberate design point worth flagging: a step's expected_outcomes are checked in two
places — when the step's own locator/action fails (e.g. "View" link doesn't exist because the
search returned no rows), AND after a step succeeds (e.g. the extract step's locator DOES
resolve nothing because the locked-member page never renders the balance table row at all, so
resolution fails there too — same code path). Both paths converge on the same
`_check_expected_outcomes` call, so there's exactly one place classification logic lives.

Verified with `scripts/smoke_test_replay.py` (real Chromium, real Flask app, a hand-authored
Capability matching exactly what `agent/compiler.py` would produce) — all 4 required scenarios
pass with real output:
1. success with member_id=23456 (never used to author the capability) — genuine parameterization
2. member_id=88888 (not seeded) → `business_outcome` / `MEMBER_NOT_FOUND`, not a crash
3. member_id=99999 (locked) → `business_outcome` / `PERMISSION_DENIED`
4. capability's entry point pointed at a nonexistent route → `hard_failure` with populated
   `failure_detail` (step_id, expected, observed) and a real screenshot saved to `/evidence/`

(That screenshot was deleted afterward — it's smoke-test output against a hand-authored fixture,
not the deliverable evidence; the real replay evidence in `/evidence/` will come from replaying
the capability actually compiled from the real discovery run, per scripts/run_replay.py.)

Also added `tests/test_replay.py` (13/13 pass) for the pure helper functions
(`_resolve_value`, `_extract_quoted_substring`, `_outcome_to_result`, `_verify_checkpoint`) using
fake page/context stand-ins, so the branching logic has offline coverage independent of the live
smoke test. Full suite: 66/66 tests pass.

## D10 — 2026-08-19 — THE real discovery run (non-negotiable requirement) — completed successfully

User provided a real `ANTHROPIC_API_KEY` (initial key had zero credit balance — user added
credits, capped at $2). Cost check before spending: Sonnet 5 is $2/$10 per 1M input/output
tokens under intro pricing through 2026-08-31 (today), and the loop is ~5 LLM turns for this
capability — comfortably inside the $2 cap even accounting for the second capability and a
short escalation demo. Key stored only in a gitignored `.env` (never committed, never printed in
full in any command output — verified `git check-ignore -v .env` before use).

Ran, for real, against the real running Flask app:

```
python scripts/run_discovery.py \
  --goal "Look up member 12345 and read their current savings balance." \
  --target http://localhost:5050/search \
  --capability-id lookup_member_balance --headless
```

Result: **succeeded on the first attempt** — no hand-holding, no hardcoded step sequence (the
smoke-test investment in D8 paid off here: the only unknown left was Claude's own tool-use
decisions, and those worked). Every one of the 5 tool calls (type/click/click/extract/finish)
came from a real Anthropic API response reasoning over a real observation; the transcript
(`evidence/discovery_run_run_672cff25c0.jsonl`, 17 lines: 1 navigate + 5 observations + 5
llm_response + 5 tool_call + 1 finish) is the record of that. Final answer: member 12345 / Dana
Whitfield / $1,842.30 — matches the seed data exactly. All 4 located steps resolved at locator
tier 1 (role_name) — consistent with the recorder smoke test, since this app's role+name pairs
are unique by design.

The compiled `capabilities/lookup_member_balance.v1.json` round-trips through
`Capability.model_validate_json`, every locator carries a real `reasoning` string, the typed
`member_id` param was correctly detected via substring match against the goal, and
`agent/compiler.py`'s declared `expected_outcomes` (MEMBER_NOT_FOUND on the View-link step,
MEMBER_NOT_FOUND + PERMISSION_DENIED on the extract step) are attached exactly as designed.

**Then replayed the real (not hand-authored) capability for all 4 required scenarios**, no LLM
involved in any of them:

1. `member_id=23456` (never used during discovery) → `success`, `savings_balance: "$5.02"` —
   genuine parameterization, not a replay of literally-recorded values.
2. `member_id=88888` (not seeded) → `business_outcome` / `MEMBER_NOT_FOUND` — not a crash.
3. `member_id=99999` (locked) → `business_outcome` / `PERMISSION_DENIED`.
4. A tampered copy of the real capability with step `s1`'s navigate URL pointed at
   `/this-route-does-not-exist` → `hard_failure`, `failure_detail` populated with `step_id: s2`,
   the expected element, "no element resolved," and a real saved screenshot. The tampered
   capability itself is saved to
   `evidence/lookup_member_balance_INJECTED_BAD_ROUTE.v1.json` for reproducibility.

All 4 results are saved as JSON to `/evidence/`. This closes out the assignment's one truly
non-negotiable requirement, plus Phase 5's full acceptance criteria, with real evidence for
every line item.

## D11 — 2026-08-19 — Phase 7 live escalation demo: a real bug found, and an honest limitation
surfaced by the real run

`scripts/demo_escalation.py` orchestrates the full real sequence: a real discovery run, a real
separate `escalation/operator_page.py` Flask process on :5001, a real HTTP GET to confirm it
shows the right context, a real HTTP POST to `/resume` (exactly what clicking the button in a
browser sends), and confirmation the discovery loop actually continues afterward.

**Goal selection, and what happened on the first attempt.** Initially tried a fabricated "wire
$50,000 to an external account" goal, expecting the agent to recognize no such feature exists
and escalate. Instead the agent did something better: it looked up the balance first, found
$1,842.30 — far short of $50,000 — and correctly finished with `business_outcome` /
`INSUFFICIENT_FUNDS`, reasoning explicitly that this was "a definitive business outcome, not a
system limitation." That's the system working *correctly* (a legitimate business outcome isn't
a failure), just not the demo needed. Switched to a goal with a real, reachable, genuinely
irreversible action instead: "open a new sub-account... and complete the account creation" —
completing (not just reaching) sub-account creation requires the final `Confirm and Open
Account` submit, which is real state-mutation (an actual DB insert). On this run the agent
reached the confirmation screen and escalated on its own, unprompted by any dead-end detection,
citing exactly the system prompt's rule: *"is a state-changing/irreversible action that creates
a new account record, so per policy I am escalating for explicit human confirmation before
submitting."* Both attempts are genuine evidence of the agent reasoning correctly about
business outcomes vs. required escalations — the first just wasn't the scenario this phase
needed, so it isn't included as a deliverable, but it's a real data point about the system
working as designed.

**A real bug, caught by this being a genuinely separate process.** The escalation demo hung
indefinitely on its first run with the corrected goal. Debugging (checking `escalation/
state/lease.json` directly, since the orchestration script's own buffered stdout showed
nothing) found the lease *had* correctly flipped to `human` with a well-formed reason — the
discovery half worked. The hang was in the operator console: `escalation/operator_page.py`,
launched via `subprocess.Popen([sys.executable, "escalation/operator_page.py"])`, crashed
immediately with `ModuleNotFoundError: No module named 'escalation'`. Running a script as
`python3 escalation/operator_page.py` puts that script's *own* directory
(`escalation/`) on `sys.path[0]`, not the project root — so `from escalation.controller import
...` can't find the `escalation` package it's sitting inside of. Every other script in this
project (`scripts/*.py`) already does `sys.path.insert(0, str(Path(__file__).parent.parent))`
for exactly this reason; `operator_page.py` was the one file that didn't, because it had only
ever been imported as a module (in the escalation tests) or run manually with cwd already at
the project root, never launched as a subprocess from elsewhere. Fixed with the same one-line
`sys.path` fix. Caught specifically *because* the demo used a real subprocess + real HTTP
instead of mocking the operator page in-process — a good argument for exercising it for real.

**The full real sequence, after the fix:** discovery escalates on its own reasoning → lease
flips to `human`, screenshot + context written to `/evidence/` → a separate Flask process
serves the operator console → a real HTTP GET confirms it shows the correct reason, current URL,
and a Resume form → a real HTTP POST to `/resume` (not a direct function call) writes the resume
signal → `trigger_escalation` unblocks, lease flips back to `automation` → the discovery loop
resumes and calls `perception.build_observation` again (fresh, not cached) before continuing.
All of this is captured in `evidence/escalation_demo_sequence.json`, plus
`evidence/escalation_run_b1712718eb.png` (the screenshot) and its context JSON.

**Honest limitation, surfaced by letting the real run play out instead of stopping once resumed:**
after resume, the agent had no way to know *what the human actually did* — approve, decline, or
something else — because the resume signal only carries a free-text summary the agent's loop
doesn't currently read back in. It re-observed the (unchanged) confirmation-screen page, wasn't
sure how to proceed without re-taking the exact action it had just escalated over, and the run
ended at `max_steps` rather than a clean `finish`. This is a legitimate stopping condition (not
a crash), and arguably a more honest result than scripting a clean finish would have been — it
surfaces a real gap: the operator page's resume signal should carry a structured outcome (e.g.
`approved` / `declined` / `did_it_myself`) that gets threaded back into the agent's next
observation, not just a human-readable note. Documented as a concrete "what I'd build next" in
REPORT.md's Cuts section, discovered by running the real mechanism rather than reasoning about
it in the abstract.

## D12 — 2026-08-19 — Closed the D11 gap: resume now carries a structured decision

D11 flagged that the resume signal only carried a free-text note, so a resumed agent couldn't
tell "approved, go ahead" from "declined, don't." This isn't just a nice-to-have — it directly
blocks Phase 8, because `open_subaccount`'s discovery goal needs the agent to actually reach and
click the final "Confirm and Open Account" submit for the compiled artifact to have a real submit
step for replay to execute (see D13). Without a way to signal approval back to the agent, there's
no way to get that step recorded by a genuine live run.

Fix: `signal_resume(human_actions_summary, decision)` now accepts `decision: "approved" |
"declined" | None` (`None` for the plain dead-end-recovery case, where approve/decline doesn't
apply). `resume()` reads the decision out of the resume signal before deleting it and carries it
forward into the fresh (state="automation") lease's `context` — the lease is the only piece of
state both the discovery loop and the operator page share, so it's the natural channel.
`escalation/operator_page.py` now shows three buttons (Approve & Resume / Decline & Resume /
Resume — no decision needed) instead of one generic Resume. `agent/discovery.py`'s `escalate`
handling now branches on `lease.context.get("decision")`: `"approved"` tells the model it has
explicit authorization to proceed with the paused action; `"declined"` tells it not to and to
conclude the run (finish with success=false or an appropriate outcome) rather than retry; `None`
just says "re-observe and decide" (the D11 dead-end-recovery case, unchanged).

4 new/updated tests in `tests/test_escalation.py` (decision propagates through
`signal_resume`→`resume`, and through the full `trigger_escalation` blocking-poll-then-resume
path) — 69/69 total, up from 66.
