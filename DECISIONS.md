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

## D13 — 2026-08-19 — Phase 8 real discovery run for `open_subaccount`; two more real bugs, found
by actually reading the compiled artifact instead of trusting it

Extended `scripts/run_discovery.py` with `--auto-approve-escalation`: it starts the real
`escalation/operator_page.py` as a separate process and, once the lease flips to `human`, posts
a real HTTP "Approve & Resume" after a short delay — this is what let a capability whose goal
requires a genuinely irreversible final step get recorded by one real, unattended run, using the
D12 decision channel. Ran, for real:

```
python scripts/run_discovery.py \
  --goal "Open a new Christmas Club sub-account for member 12345 with a $50 opening deposit, \
          and complete the account creation." \
  --target http://localhost:5050/search --capability-id open_subaccount \
  --max-steps 12 --auto-approve-escalation --headless
```

The agent searched, opened member 12345, navigated to the sub-account form, filled only the
opening deposit (it left account type at its default "Christmas Club" — the first `<option>` —
and left the optional nickname blank, a sensible minimal path), reached the confirmation screen,
and — unprompted — escalated citing the exact same reasoning as the D11 demo. The auto-approve
watcher posted a real "approved" decision; the agent then genuinely clicked "Confirm and Open
Account" *inside the iframe* (`tier: role_name` in the log — confirming the recorder's
cross-frame resolution works during compilation, not just perception) and finished successfully:
sub-account #2 created for Dana Whitfield. Transcript:
`evidence/discovery_run_run_8ee69bcbf8.jsonl`.

**Then, reading the compiled `capabilities/open_subaccount.v1.json` closely (not just checking
it parsed) surfaced two real bugs:**

1. **Redaction corrupted a schema field.** `output_schema.sub_account_number` came out as the
   literal string `"***REDACTED***"` instead of `{"type": "string"}`. Cause: `save_capability`
   ran `redact()` over the *entire* `capability.model_dump()`, and `redact()` matches keys by
   substring — `sub_account_number` contains `account_number`, one of the configured secret
   markers, so its whole value (a schema-type dict, not data) got replaced. Root problem:
   `input_schema`/`output_schema` are pure type metadata and never carry real data — there was
   nothing there to protect, and redacting them corrupts the artifact's structure instead. Fixed
   `agent/compiler.py::save_capability` to redact only `steps` (the one place a literal value the
   LLM actually typed could appear), never the schema dicts. 2 new regression tests.

2. **Parameter detection collided on a coincidental substring — a real correctness bug, not just
   corrupted metadata.** Step `s6` (typing `"50"` into "Opening Deposit ($)") was recorded as
   `value: {"param_ref": "member_id"}`, not the literal `"50"`. Cause: the recorder's substring
   check (`if value and str(value) in self.goal`) doesn't check *which* value it found — the
   goal text "...member 12345 with a $50 opening deposit..." contains "50" as a substring (inside
   "$50"), so the deposit amount got misattributed to the member_id parameter. Had this shipped,
   replaying with a different `member_id` would have typed that member_id into the deposit
   field instead of $50 — silently wrong money, not just a cosmetic issue. Fixed
   `agent/recorder.py`: the member ID is now extracted from the goal *once*, via a fixed
   `member (\d+)` pattern, and a typed/extracted value is only tagged `param_ref` if it *exactly
   equals* that extracted ID — not "appears anywhere in the goal." 3 new regression tests,
   including the literal deposit-amount collision as a named test case.

**Both bugs were repaired directly in the already-generated real artifact** (rather than
re-spending API credits on a third discovery run for something that doesn't require new agent
reasoning): `output_schema.sub_account_number` restored to `{"type": "string"}`, and step `s6`'s
`value` restored to the literal `"50"` — verified against the real transcript's `tool_call` entry
(`{'role': 'textbox', 'name': 'Opening Deposit ($)', 'text': '50'}`), so the repair matches
exactly what the fixed code would have produced from the same real run. The discovery run itself
was never faked or replayed — only the serialization bugs it exposed were fixed, in the code and
in this one artifact. Full suite: 74/74 tests pass (up from 69).

This is the clearest example in this build of why "round-trips through
`Capability.model_validate_json` with no errors" (the spec's literal compiler acceptance
criterion) is necessary but not sufficient — both bugs produced perfectly schema-valid JSON.
Nothing caught them until the actual field values were read and checked against what the real
run actually did.

## D14 — 2026-08-19 — A third real bug: a declared business outcome on the wrong step

Ran the full replay verification suite for `open_subaccount` (success with a new member,
`confirm=True` required, both business outcomes) matching D10's rigor for
`lookup_member_balance`. `member_id=23456` (never used to record) with `confirm=True` genuinely
created sub-account #3 — confirmed directly against `app/bank.db`, not just trusting the
`status: success` result. `member_id=88888` (not seeded) correctly returned `business_outcome` /
`MEMBER_NOT_FOUND`. But `member_id=99999` (locked) came back `hard_failure` at step `s5` (`Open
sub-account` link not found) instead of the expected `business_outcome` / `PERMISSION_DENIED`.

Checked the real page: `curl http://localhost:5050/member/99999` — `member_detail.html` renders
only the `msg-denied` branch for a locked member; it never emits an `Open sub-account` link at
all, only for the active/found branch (see `app/templates/member_detail.html`). The
`_KNOWN_OUTCOMES` entry in `agent/compiler.py` had declared `PERMISSION_DENIED` on the
`"Continue"` button click (step `s7`) — reasoning from `app.py`'s server-side check (locked
members get turned away when *submitting* the new-subaccount form) rather than from what the UI
actually does. But a locked member's flow never reaches `s7` at all: it dead-ends one click
earlier, at `s5`, because the link to even *reach* the form was never rendered. Moved the
declaration to `s5` (same step that already carries `MEMBER_NOT_FOUND`, for the analogous
"the link this step needs doesn't exist" reason), corrected the condition text to match
`member_detail.html`'s actual locked-branch copy ("Access denied. This account is restricted"),
and removed the stale declaration from `s7`. Patched the same fields directly in the real
`capabilities/open_subaccount.v1.json` (verified via round-trip), added a regression test
(`test_open_subaccount_permission_denied_attaches_to_the_open_subaccount_link_not_continue`),
and re-ran the replay for real: now correctly returns `business_outcome` / `PERMISSION_DENIED`.

All three D13/D14 bugs share a root cause worth naming: `agent/compiler.py`'s declared
`_KNOWN_OUTCOMES` are authored from *domain knowledge of the app*, not from what the one real
discovery run actually observed (see `agent/compiler.py`'s own docstring on this trade-off) —
which means they're exactly as reliable as that domain knowledge, and wrong domain knowledge
produces a schema-valid but behaviorally wrong artifact that nothing catches except actually
replaying every declared branch against the real app. This build did that for both capabilities'
full outcome matrices; a production version would want this as a standing test suite run on
every new capability version, not a one-time manual check. Full suite: 75/75 tests pass.

## D15 — 2026-08-19 — README.md, REPORT.md, and a genuine fresh-clone verification

Wrote `README.md` (setup, offline-vs-live guidance, exact demo commands for both capabilities)
and `REPORT.md` (the 7 required headings, written against what was actually built and verified,
not the original plan). Then did the assignment's explicit "verify it clones clean and the
README's setup steps actually work from a fresh clone" check for real: `git clone`d this repo
into `/tmp/fresh_clone_test`, confirmed no `.env`/`.venv`/`bank.db`/`__pycache__`/browser-profile
files leaked into git history (only source, tests, capabilities, and evidence), ran the README's
exact setup commands (`pip install -r requirements.txt`, `playwright install chromium`), ran the
full test suite (75/75 pass), started the mock app fresh, and replayed the real
`lookup_member_balance.v1.json` capability with `member_id=34567` — a THIRD member ID never used
in any prior test in this build, and never used to record — correctly returning `$9,901.00`
(matching seed data) with no API key set anywhere in that clone. This closes the loop on
"deterministic replay needs no LLM" and "genuine parameterization" one more time, independently,
from a location that has never seen any of this project's local state.

## D16 — 2026-08-19 — Real bug found via an actual human using the escalation UI: wall-clock
timeout counted human review time against the run's own budget

Ran the full interactive escalation flow with an actual human (not the automated
`--auto-approve-escalation` stand-in): a real discovery run for `open_subaccount`, non-headless,
no auto-approval — genuinely waiting for a person to open `escalation/operator_page.py` and
click a button. The person briefly opened the wrong page (`localhost:5050`, the bank app itself,
instead of `localhost:5001`, the operator console — an easy mix-up between "the page the
automation controls" and "the page the human controls"), then found the right one and clicked
**Approve & Resume**. The lease flip worked correctly (`decision='approved'` recorded exactly
right) — but the overall run then reported `status=timeout`, discarding a run that had actually
been approved and was ready to finish.

Root cause, found by reading `agent/discovery.py`'s loop: `start_time = time.monotonic()` is set
once at the top of `run_discovery` and never adjusted. `trigger_escalation` **blocks** — by
design, since a human might take real time to review — but the wall-clock check
(`time.monotonic() - start_time > timeout_s`) doesn't know the difference between the agent
actively working and the agent sitting idle waiting on a person. The default `timeout_s=300s`
(5 minutes) is generous for the agent's own pace, but a person reading an unfamiliar escalation
screen and finding the right URL for the first time can easily take longer than that — and every
second of that gets silently charged against the same budget.

This is a real design flaw the assignment's own "stopping conditions" section implicitly assumes
away: a wall-clock timeout is supposed to catch a run that isn't making progress, not penalize a
human for taking their time to make a good decision. Fixed in both places `trigger_escalation` is
called (the dead-end path and the model-invoked `escalate` tool): capture `time.monotonic()`
immediately before and after the blocking call, and add the delta to `start_time` — this
shifts the timeout's clock forward by exactly however long the human took, so only genuine
agent-working time counts against `timeout_s`.

Verified with a new regression test, `scripts/smoke_test_escalation_timeout.py`: runs the real
loop (real browser, real Flask app, real `escalation/controller.py`, only the Anthropic client
faked) with a deliberately short `timeout_s=3.0` and an escalation wait engineered to last 6
seconds — longer than the timeout. Real output: total elapsed 7.2s, and the run still finished
`status=success`, proving the fix. Full suite: 75/75 unit tests still pass.

Notable: this bug was invisible to every automated demo run in this build (`demo_escalation.py`,
`--auto-approve-escalation`), because the scripted approver always resumed within ~2 seconds —
comfortably inside the 300s budget. It only surfaced once an actual human used the actual UI at
actual human speed, which is exactly the kind of thing "at least one genuine run" catches that a
scripted stand-in cannot.

## D17 — 2026-08-20 — Value-shape redaction, and correcting the compliance framing (GLBA, not HIPAA)

User asked about HIPAA compliance. Corrected: HIPAA is healthcare-specific (protected health
information); the framework that actually applies to a US bank is **GLBA** (the Safeguards Rule),
plus general state/consumer privacy law — the assignment itself already frames this correctly as
"regulated financial data," not HIPAA. Worth stating precisely since citing the wrong regulation
in front of reviewers would read as a miss regardless of how solid the underlying engineering is.

The substantive question underneath — "what the artifact sees shouldn't go out either" — was a
real, concrete gap, confirmed by checking an actual evidence file: `evidence/discovery_run_
672cff25c0.jsonl` contains `"Dana Whitfield"` and `"$1842.30"` in plaintext, committed to the
public repo. Harmless here (synthetic seed data), but if this exact code ran against a real bank,
that log would contain a real customer's real name and balance, permanently, in plaintext.

Important nuance worked through before changing anything: the name and balance are **not** a
redaction bug in the usual sense — `lookup_member_balance`'s entire job is to return that balance
to the calling agent, so it legitimately belongs in `Result.outputs`. Blanket-redacting names or
currency-formatted values would break the system's actual purpose. The real, narrower gap:
`redact()` only ever matched by *key name* (`ssn`, `password`, etc.) — a secret sitting in an
unrelated, innocuously-named field (a free-text log line, an observation payload) would sail
through untouched, regardless of its shape.

Fixed: added a second redaction pass in `guardrails/policy.py`, `_contains_structured_secret`,
matching SSN (`###-##-####`) and card/routing-number-shaped digit runs (13-19 digits, optional
space/dash separators) in string *values*, independent of key name. Deliberately narrow — these
two shapes are distinctive enough to flag with very low false-positive risk; this is explicitly
not a general PII scanner (a name isn't secret-*shaped*, full NLP-based PII detection stays a cut).

Verified the false-positive risk directly rather than assuming it away: ran `redact()` against
every real entry in an actual discovery transcript (0/17 changed) and against 1,000 randomly
generated SHA256 hashes (0/1000 falsely flagged) — the pattern's `\b` word-boundary anchors only
fire at genuine token edges, not mid-hash, since a hex string's letters (a-f) break word-boundary
continuity, so a 13+ digit-only run essentially never survives inside one by chance. Also
confirmed directly that member IDs, currency-formatted balances, and customer names are correctly
left untouched (5 new tests in `tests/test_guardrails.py`, 80/80 total pass).

Also updated `REPORT.md`'s Safety section to name the redaction improvement, the corrected GLBA
framing, and an honest remaining gap: this build persists full transcripts to `/evidence/`
indefinitely (required for the assignment's demonstration), but a real deployment would need
those treated as regulated data at rest — encryption, access control, a retention window — not
committed permanently to a public repo. That's a deployment/ops policy, not new infrastructure to
build here.

## D18 — 2026-08-20 — Closing the two real, fixable weak links: discovery/replay domain
separation, and operator console authentication

Following up on D17's GLBA discussion, user asked to actually fix whatever was fixable rather
than just document gaps. Went through the three named gaps and made a real call on each:

1. **Encryption at rest** — left as a documented cut, deliberately. A hardcoded local key would
   be security theater, not a real fix (no real key management, no rotation) — worse than being
   honest that this needs actual KMS infrastructure a take-home shouldn't build.
2. **Discovery-time data exposure to a third-party LLM** — fixed as an *enforced* control, not
   just a stated rule. Added `discovery_allowed_domains` to `guardrails/allowlist.yaml`, separate
   from (and here, matching) `allowed_domains`. `guardrail_check` now takes `phase="discovery"|
   "replay"` and checks the matching list — `agent/discovery.py` passes `phase="discovery"`,
   `replay/engine.py` passes `phase="replay"`. This means "discovery must never touch a
   production domain" is now something the code itself would refuse, not just a convention a
   future engineer could forget — adding a production domain to `allowed_domains` for replay
   (which never calls the LLM) does NOT automatically permit discovery there. 4 new tests.
3. **Operator console has zero authentication** — the one judged most safety-critical (whoever
   can reach the page can approve an irreversible financial action) and genuinely fixable at
   reasonable scope. Added HTTP Basic Auth to `escalation/operator_page.py` via `@app.
   before_request`, checked with `secrets.compare_digest` (timing-safe). Fail-secure design: if
   `OPERATOR_PASSWORD` isn't set, a random one-time credential is generated and printed to the
   console's own terminal at startup — it never silently serves unauthenticated, but also never
   hard-fails a fresh checkout with no setup step. `scripts/run_discovery.py`'s
   `--auto-approve-escalation` and `scripts/demo_escalation.py` (both of which launch the
   operator console as a subprocess and talk to it over real HTTP) now generate a credential and
   pass it to the subprocess via `env=`, then include it as a real Basic Auth header on every
   request — the same code path a real operator's browser would use, not a bypass.

Verified with 7 offline tests (`tests/test_operator_page.py`, using Flask's test client) AND a
live integration smoke test (`scripts/smoke_test_operator_auth.py`) that launches the real
subprocess and drives it over real HTTP — because trusting the plumbing without running it has
already produced real bugs twice this session (D16, and the venv/conda issue). The live test's
first run genuinely failed: `unauthenticated GET / is rejected with 401` came back FAIL. Root
cause, found via `lsof -i :5001`: a stale `operator_page.py` process from an earlier interactive
demo in this same session was still bound to port 5001, running the *old* pre-auth code — the new
subprocess couldn't bind, so every request silently hit the old unauthenticated instance instead.
Not a bug in the new code; killed the stale process and re-ran clean. All 8 checks then passed,
including the one that matters most: an unauthenticated `/resume` POST is rejected with 401 AND
the lease provably stays in `state: human` afterward — it cannot be used to sneak an approval
through.

91/91 tests pass (91 = 80 + 4 domain-separation + 7 operator-auth).

## D19 — 2026-08-20 — Reconsidered encryption at rest: it IS fixable, built it for real

User pushed back on D18's "encryption at rest is a documented cut, a hardcoded key would be
theater" reasoning by asking directly: can't that last one be fixed? Re-examined the reasoning
and found it was too conservative. The actual objection was never "a key in a local file is
inherently theater" — it's specifically that a key baked into *source code* would be. A key
sourced from `EVIDENCE_ENCRYPTION_KEY` in `.env` is the exact same trust model this project
already uses for `ANTHROPIC_API_KEY` and `OPERATOR_PASSWORD` (D18) — treating that as acceptable
for two secrets and "theater" for a third was an inconsistent standard, not a principled one.

Built `guardrails/encryption.py`: `encrypt_at_rest`/`decrypt_at_rest` using `cryptography`'s
`Fernet` (AES-128-CBC + HMAC-SHA256, authenticated — tampering is detected, not silently
accepted) — a real, vetted library, never custom crypto. `generate_key()` for setup.
Fail-closed, matching the operator console's D18 posture: both functions raise
`EncryptionKeyMissing` rather than silently writing plaintext if no key is configured.

The one real constraint that shapes where this gets applied: the assignment requires `/evidence/`
to stay human-readable so reviewers can actually check it. Encrypting this repo's own deliverable
evidence would defeat that requirement, not serve compliance — so this is deliberately NOT wired
into `agent/compiler.py::save_capability` or the evidence-writing paths in `scripts/run_discovery.
py`/`replay/engine.py`. Instead, proved the capability is real and working via
`scripts/demo_encryption_at_rest.py`, run for real: writes a customer-shaped record (member name
+ balance) to an actual file, confirms neither value — nor even valid JSON structure — survives
on disk, then decrypts it back byte-for-byte identical. This is the thing a real deployment's
evidence/capability write paths would call; here it exists to prove the capability rather than
be wired into the submission's own reviewable output.

7 new offline tests (`tests/test_encryption.py`): round-trip, ciphertext genuinely doesn't
contain the plaintext, fails closed with no key configured (both directions), wrong key raises
`InvalidToken` rather than decrypting into garbage, and bit-flip tampering is detected (Fernet's
authentication, not just its encryption). 98/98 tests pass.

Honest limitation, stated plainly rather than glossed over: one static key, no rotation, no
per-tenant/per-record keys, no HSM-backed custody. This is a credible first step toward real
encryption at rest, not a finished KMS — that remains genuinely out of scope for a take-home, and
is named explicitly as "what I'd build next" in REPORT.md rather than left implicit.

## D20 — 2026-08-20 — Added a transactions/dispute feature to the mock app, to actually
demonstrate discovery on a genuinely new goal type (not required by the assignment)

User asked what happens when discovery is given a goal type the system has never seen before
(e.g. "get the latest transaction," "dispute a transaction") — beyond the assignment's two
required capabilities. Answered conceptually first (`agent/discovery.py` is fully goal-agnostic;
nothing about it is specific to balance-lookup or sub-account-creation), then the user asked to
actually build the feature so this could be demonstrated for real rather than argued abstractly.

Added, following the same deliberately-hostile-markup conventions as the rest of the app
(nested tables, non-semantic classes, real semantic `<th scope=row>`/`<button>` underneath):
- `transactions` table (`app/models.py`): `member_id`, `txn_date`, `description`, `amount_cents`,
  `status` (`posted`/`disputed`), `dispute_reason`. Seeded newest-first per member so "the latest
  transaction" is always the first row — a read-only capability target.
- `GET /member/<id>/transactions` — a data table (not the label/value `th`/`td` shape the
  existing two capabilities use) with a "Dispute" link per posted row — a genuinely different UI
  shape from anything discovery has driven before, and a real test of whether the existing
  locator/extraction design generalizes.
- `GET`/`POST /member/<id>/transactions/<txn_id>/dispute` — a state-mutating action (marks a
  transaction disputed) — a second natural `risk_level: risky` candidate.
- Reused `error_permission.html` for the locked-member case on both new routes — surfaced a real
  content bug in doing so: its message was hardcoded to say "sub-account creation is not
  permitted" even when the actual blocked action was viewing transactions. Generalized the
  copy ("this action is not permitted") since the template is now shared across three contexts.

Verified via curl (list renders, dispute form shows the right transaction, submitting flips
status to `disputed` and the list reflects it, locked member blocked with the corrected message,
not-found member handled) before spending anything on a live discovery run. Full test suite
re-run and still green (98/98) — this app change doesn't touch anything the existing unit tests
exercise directly.

## D21 — 2026-08-20 — A genuine, naturally-occurring limitation: extraction from a data-table row
has no stable anchor, and replay fails because of it

Ran a real, unscripted discovery against the new transactions page: goal "look up member 12345
and report their most recent transaction (date, description, amount)." Succeeded on the first
try, `status=success`, and — genuinely interesting — it correctly generalized to a page structure
none of the existing capabilities use: it found and clicked "View Transactions" (a link that
didn't exist when either prior capability was recorded) and read data out of a plain data table
(date/description/amount/status as columns) rather than the label/value `<th>`/`<td>` shape both
existing capabilities rely on. Zero code changes were needed for this — confirms the design
claim in `REPORT.md` §1 that `agent/discovery.py` is fully goal-agnostic.

**But replaying the compiled artifact failed for real, immediately, on the very first check —**
not injected, not contrived. `capabilities/lookup_latest_transaction.v1.json`'s step `s6`
(the only `extract` step) has locator `{"role": "cell", "name": "2026-08-15"}` — the LLM anchored
on the transaction date's own literal value, because a data table row has no separate stable
label the way `<th scope="row">Savings Balance</th>` gives the balance lookup one. Replaying
against `member_id=23456` (whose latest transaction is dated `2026-08-12`, not `2026-08-15`)
immediately returned `hard_failure`: no cell with that exact text exists on that member's page.
The one thing the recorder anchored on was exactly the thing guaranteed to differ between runs —
the value being extracted, not something identifying where to find it.

A second, related gap the same run exposed: `finish()`'s `outputs` had 4 fields (`member_name`,
`most_recent_transaction_description`, `most_recent_transaction_amount`, `status`) that the model
read directly off the observation without ever calling the `extract` tool on them — so the
compiled `output_schema` declares 6 outputs, but only 1 (`most_recent_transaction_date`) has a
recorded `Step` that could reproduce it on replay. The other 4 would silently come back missing
even if step `s6`'s locator hadn't failed outright.

**Root cause, in one sentence**: the recorder's locator strategy (and the `extract` tool's
underlying row-relative heuristic in `agent/tools.py`/`replay/engine.py`) was built around
*label → value* pairs, where the label is a stable UI element and the value is the thing that
changes. A data-table row (columns, no per-cell label) doesn't have that shape — there's nothing
stable to anchor on except the value itself. This is a real architectural gap, not a bug in the
sense of "code doing the wrong thing" — the code did exactly what it was designed to do; the
design just doesn't cover this UI shape yet.

**Deliberately not silently fixed or hidden.** This is the single clearest illustration in this
whole build of the assignment's own stated goal ("we care more about clear thinking, sound
trade-offs... than breadth of features") — a real limitation, found by actually running the
system on a case it wasn't built for, is worth more than a broader feature set that avoids ever
hitting one. Real failure evidence: `evidence/replay_lookup_latest_transaction_member_id-23456.
json` (the `hard_failure` result) and `evidence/replay_replay_1787208576142_failure_s6.png` (the
screenshot). A real fix would need a new locator strategy — something like "Nth cell of the
current/topmost row of table X, addressed by column position rather than content" — which is a
genuine schema/recorder/replay change, not a one-line patch; noted as a concrete next step in
REPORT.md rather than attempted under time pressure just to make this one demo pass.

## D22 — 2026-08-20 — Actually built the D21 fix, and it surfaced two more real, related gaps
along the way

User asked to fix D21's finding for real rather than leave it as a documented limitation. Built
three fixes, verified live end to end, in order of discovery:

**1. A fourth locator strategy: `table_position`.** New `LocatorTarget.strategy` value
(`artifact/schema.py`). `agent/recorder.py::_try_table_position_locator` detects the exact shape
that broke in D21 — a `<td>` cell with no per-row `<th>` label, inside a table that *does* have
`<th scope="col">` column headers — and instead of anchoring on the cell's own value, walks the
real DOM (`ancestor::tr`, `ancestor::table`, a JS `evaluate` counting preceding siblings) to
record `{table_headers, row_index, column_index}`: which table (identified by its headers, which
don't depend on data), which row, which column. `replay/engine.py::_locate_table_position`
resolves it the same way at replay time. Verified live with a real browser and two versions of
the same table shape with completely different data (`scripts/smoke_test_table_position.py`):
building against one page's row 0 and resolving against a different page's row 0 correctly
returns *that* page's own value, not the original. Then re-ran the real, live discovery for
`lookup_latest_transaction` — the tier log now shows `table_position` used automatically, no
prompting — and replaying the fresh artifact against member 23456 (the exact case that failed
with `hard_failure` in D21) now returns `status=success` with that member's *actual* latest
transaction date (`2026-08-12`, correctly different from the discovery member's `2026-08-15`) —
real proof, not just a passing unit test.

**2. A second, unrelated real bug the same live run exposed: the default checkpoint was
backwards.** `scripts/run_discovery.py`'s fallback for any `capability_id` not in the hardcoded
`CHECKPOINTS` dict was `Checkpoint(type="url_match", expected=args.target)` — checking that the
*final* page equals the *starting* page. That's wrong for essentially every real capability,
since the entire point of running one is to navigate somewhere else; replaying the fresh
transaction capability failed at the checkpoint step even though every actual step (including
the new `table_position` extract) had already succeeded. Fixed with `_default_checkpoint`: use
the real final URL's last path segment (e.g. `transactions`, from `/member/12345/transactions`)
instead of the full starting URL — a `url_match` substring check against just that segment
correctly matches a differently-parameterized replay URL (`/member/23456/transactions`) without
needing to understand the route's parameterization. 3 new offline tests
(`tests/test_run_discovery.py`) plus re-verified live.

**3. A third real gap, once both of the above were fixed and replay finally reached real data
checks: an empty-state placeholder silently reported as `success`.** Replaying against member
34567 (seeded with zero transactions) returned `status=success`,
`outputs={'most_recent_date': 'No transactions on file.'}` — `transactions.html`'s empty-state
row is still "row 0, column 0" of the table, so the position-based locator resolved to it
without knowing it wasn't real data. This is precisely the failure mode the assignment names as
"the most common design mistake here" (business outcome silently treated as success), just
appearing from the opposite direction of the label/value cases: there the *locator* failed to
resolve and the fix was checking `expected_outcomes` on failure; here the locator resolved fine
to the wrong *kind* of content. Fixed the same declarative way as every other business outcome
in this build: added a `lookup_latest_transaction` entry to `agent/compiler.py`'s
`_KNOWN_OUTCOMES` — `condition="page contains 'No transactions on file.'"`,
`classification="business_outcome"`, `code="NO_TRANSACTIONS"` — on the extract step, which
`replay/engine.py` already checks *after* a successful action, not only on failure (the same
mechanism D13/D14 established). Re-verified live: same replay now returns
`status=business_outcome`, `business_outcome_code=NO_TRANSACTIONS` instead of a bogus success.

**Also fixed, found while re-verifying:** `agent/compiler.py::infer_output_schema` used to
declare every key `finish()` reported, whether or not any step actually extracted it — the exact
"4 of 6 declared outputs have no backing step" half of D21's finding, still true even after the
locator fix. Now takes `steps` and only declares a key if some step's `extract_as` matches it,
printing a warning (never silently) for anything dropped. One test needed a real fix rather than
a signature update: `test_save_capability_does_not_corrupt_output_schema_with_a_secret_like_field_name`
asserted an *unbacked* `sub_account_number` key survived redaction — under the new, correct
behavior it's dropped before redaction ever sees it, so the test's `FakeRecorder` now includes a
real extract step backing that key, preserving the test's actual intent (redaction doesn't
corrupt output_schema) without relying on the now-fixed over-declaration bug.

All three artifact fixes (table_position + checkpoint + output_schema) were also manually
reapplied to the already-compiled `capabilities/lookup_latest_transaction.v1.json` — verified via
round-trip and three full live replays (success for two different members with different real
data, `business_outcome`/`NO_TRANSACTIONS` for the member with none) — same discipline as D13/D14:
repair the artifact to match what the fixed code now produces, backed by real transcript/replay
output, rather than re-spend API credits on a fourth discovery run for schema-only corrections.
`lookup_latest_transaction` is now a third fully-verified capability, to the same standard as the
two the assignment actually requires. Full suite: 108/108 tests pass (up from 98).

## D23 — 2026-08-20 — Gave reviewers a genuinely undiscovered feature to point discovery at, and
it immediately surfaced a real risk-classification gap

User asked what happens if a reviewer wants to make discovery learn something it's never seen,
rather than just replaying the three pre-baked capabilities — and asked for the mock app to have
more real variety to demonstrate that on. Two changes:

**1. Added `update_member_address`, a genuinely new mock-app feature with zero pre-existing
capability.** `app/models.py` gained `address_line1`/`city`/`state`/`zip_code` columns and
`update_member_address()`; `app/app.py` a `GET/POST /member/<id>/update-address` route; two new
templates in the same hostile-but-semantic style as the rest of the app. Deliberately plain text
inputs only, no `<select>` — the discovery agent's action vocabulary is `click`/`type`/`navigate`/
`extract` with no dropdown primitive (documented as a new cut in `REPORT.md`), so a goal against a
`<select>` field would silently have no tool available to act on it. `dispute_transaction` (added
in D20) already had zero compiled capability too, so reviewers now have two real, untouched
features to choose from, of genuinely different shapes.

**2. Ran discovery live against `update_member_address` to prove the story is real before writing
it up — and it surfaced a real bug.** The model correctly judged submitting the address change as
a state-changing action and called `escalate()` on its own judgment (not scripted), exactly per
the system prompt's own rule. Approved it via `escalation.controller.signal_resume()` (the same
call the operator console's Approve button makes), the run resumed and finished, and
`agent/compiler.py` compiled `capabilities/update_member_address.v1.json` —
but with `risk_level: "safe"`. `scripts/run_discovery.py`'s `RISK_LEVELS` is a static dict keyed by
`capability_id`, hardcoded for only the two capabilities the assignment names
(`lookup_member_balance`, `open_subaccount`); anything else — including a capability whose own
discovery run needed a human to approve an irreversible step — silently defaulted to `"safe"`.
That default is exactly what would let replay execute a real address-changing action later with
*zero* `--confirm` gate, on any newly-discovered capability the author forgot to add to the table.
Fixed with `_infer_risk_level(capability_id, transcript)`: explicit table entries still win, but
anything else now inherits `risky` if `escalate_requested` appears anywhere in that run's own
transcript, `safe` otherwise — the run's own history decides, not a static list someone has to
remember to update. Manually patched the already-compiled artifact's `risk_level` to match (same
D13/D14/D22 discipline — backed by that run's real transcript, not a re-run), then verified live
both directions: replay without `--confirm` now correctly returns `hard_failure` with
`"confirm=True for a risky capability"`; with `--confirm` it executes and the member's address
changes for real. 3 new offline tests in `tests/test_run_discovery.py`. Full suite: 111/111 tests
pass (up from 108).

**One more honest limitation this surfaced, left as-is and documented rather than fixed:**
replaying `update_member_address` against a *different* member_id correctly re-targets that member
(the same `member_id` param-detection every other capability uses), but writes the *same* address
values recorded during discovery, not new ones — parameter detection (D13's fixed
`member (\d+)`-only, exact-match slot-filler) has no way to generalize arbitrary form field values,
only the member ID. Confirmed live: replaying against member 45678 with `--confirm` gave them
Denver, not a new address of their own. This is the same already-documented cut as D13/REPORT.md's
Cuts section, just now visible on a capability outside the two the assignment requires — reviewer
docs describe this replay as "set this same recorded address for a different member," not "set an
arbitrary new address," so nobody is misled by trying it.

**Postscript, same day:** re-ran the whole story a second time, independently, on the *other*
previously-uncompiled feature — `dispute_transaction`, goal "File a dispute for member 12345's
most recent transaction, reason 'unauthorized charge'." Same shape held: the model escalated on
its own judgment before submitting, a human approved it through the real operator console UI in a
browser (not `signal_resume()` called directly this time), the run resumed and finished, and
`capabilities/dispute_transaction.v1.json` compiled with `risk_level: risky` — this time with zero
manual patching, since the `_infer_risk_level` fix was already live in the code. Confirmed the
dispute actually wrote to the database (`transactions.status` -> `'disputed'`). Both previously-
uncompiled features are now real, compiled, risk-correct capabilities — `capabilities/` holds 5
files, not the 2 the assignment requires. This also means the "two genuinely untouched features"
framing in `README.md`/the reviewer artifact is now stale (both are compiled); updated it to be
honest about that and tell reviewers how to get a truly blank slate themselves: delete the
specific `capabilities/<id>.v1.json` first, then discover it fresh.

## D24 — 2026-08-20 — Operator console could show a resolved escalation as if it were still
pending

Found by the user running the interactive escalation walkthrough themselves (the `open_subaccount`
flow, approving via the real browser UI at `localhost:5001`): after clicking Approve, they saw the
Chromium automation window close (expected — the run finished successfully; a real sub-account was
created, confirmed via the DB directly). But they also described clicking Approve a second time and
seeing "the same thing" — the identical escalated-request view — before it closed again.

Root cause: `escalation/controller.py::resume()` genuinely does flip the lease back to
`state="automation"` immediately (verified — not the bug), but the Flask response for `GET /` never
told the browser not to cache it, so a reload or back-navigation could redisplay the old, already-
resolved "escalated" view straight from the browser's own cache. A second click of "Approve &
Resume" against that stale page is harmless in practice (it only writes a resume-signal file
nobody is polling for anymore — confirmed directly against the DB: exactly one sub-account row was
created, not two), but it's a real UX gap: an operator has no way to tell a genuinely-still-pending
request apart from a stale cached view of an already-resolved one, and for an action this
consequential that ambiguity matters. Two fixes in `escalation/operator_page.py`: (1)
`Cache-Control: no-store, no-cache, must-revalidate, max-age=0` on every response, so the page
always reflects live lease state; (2) `/resume` now redirects to `/?resumed=<decision>`, which
renders a plain confirmation banner ("Resume signal sent...") — positive feedback that the click
actually did something, instead of a bare redirect that gives an operator no way to distinguish
"it worked" from "did that go through?". 2 new tests in `tests/test_operator_page.py` (9/9 pass).
Full suite: 111/111 (no count change — this fix needed no new capability-side tests).

## D25 — 2026-08-20 — Auto-open the operator console the moment a run actually escalates

User asked, playing the banker role themselves: how would an operator even know a run escalated,
or where to look, without watching the terminal scrollback for the `ESCALATED` line and
remembering port 5001? Real friction a human operator wouldn't tolerate.

New `--open-console-on-escalation` flag (`scripts/run_discovery.py`). If the port isn't already
occupied, spawns `escalation/operator_page.py` itself (same subprocess pattern
`--auto-approve-escalation` already used, but printing credentials instead of consuming them) and
starts a background watcher thread. The watcher's actual open/re-arm decision is factored into a
pure function, `_console_watcher_step(lease_state, already_opened) -> (should_open, new_state)`,
specifically so it's unit-testable without real threads or sleeps — opens exactly once per
escalation via `webbrowser.open()`, and re-arms after the lease resolves so a second escalation
later in the same run reopens it too. No run ID to hunt for: `read_lease()` always shows whatever
is currently pending, and there's only ever one. Verified live, for real: ran a discovery goal
that escalates with the flag on, and the operator console's own Flask log showed a genuine
`GET / HTTP/1.1" 401` — proof a real browser tab opened itself and hit the console before any
credentials were entered, not just that the code path executed. 6 new offline tests (2 for
`_port_is_open`, 4 for `_console_watcher_step`). Full suite: 119/119.

## D26 — 2026-08-20 — `recoverable`'s own docstring described behavior the code never implemented

Re-checking against the assignment's evaluation criteria ("how cleanly it separates... recoverable
conditions") surfaced a real doc/code mismatch: `replay/engine.py`'s module docstring claimed
`recoverable_handled` meant replay "handled it (dismiss/retry) and kept going," but the actual
code (`_outcome_to_result`) just returns a terminal `Result` — identical short-circuit behavior to
`business_outcome`, no retry, no continuation. Also confirmed live: none of the 5 real capabilities
declare a `recoverable` outcome at all, only exercised via `tests/test_replay.py`.

Decided not to build real in-place retry to match the old docstring — a replay engine that
silently retries an unrecognized page state is the wrong instinct for a banking system, and this
mock app's business logic is fully deterministic with no naturally-occurring transient state to
retry against; fabricating one just to exercise the path would mean building non-deterministic app
behavior, directly against the "replay must be deterministic" requirement. Instead corrected the
docstring to describe what the code actually and correctly does: a terminal status distinct from
`hard_failure`, telling the *caller* "safe to retry the whole run later" rather than "something's
broken, go investigate." Same clarification added to `REPORT.md` §3 so a reviewer doesn't have to
find the mismatch themselves. No behavior change; docs-only fix. Full suite: 119/119.

## D27 — 2026-08-20 — Stretch goal: agent-facing capability interface, and a real bug the very
first live run found

Picked one stretch goal (assignment's own "depth over breadth" guidance, at most one or two):
expose `capabilities/*.json` as a catalog an AI agent can discover and invoke by name with typed
args. New `agent_interface/` package:

- **`catalog.py`** — `build_tool_catalog()` maps each real `Capability` straight to a Claude
  tool-use shape. `input_schema` is already `{param: {"type", "description"}}`, exactly the
  JSON-Schema `properties` shape a tool needs, so this is a direct mapping, not a translation
  layer that could drift from what `replay()` actually accepts.
- **`invoke.py`** — `invoke_capability(capability_id, args, confirm=False)` routes to the real
  `replay()`. Deliberately does **not** expose `confirm` as a tool-schema field the LLM can set:
  that gate exists so a human (or code a human configured) decides whether an irreversible action
  proceeds — letting the model set `confirm=True` itself on a tool call would silently defeat the
  exact guardrail `check_risk_confirmation` already enforces server-side. `confirm` is a parameter
  of the Python function, invisible to the model.
- Added `Capability.description` (schema addition, `artifact/schema.py`) — was genuinely missing:
  nothing previously carried a human/agent-readable summary of what a capability *does*, only its
  typed I/O. Populated from the original discovery `--goal` text (`compile_capability`); manually
  patched onto all 5 already-compiled artifacts using the exact goal each was recorded from
  (traced through their own evidence transcripts), verified via round-trip validation — same
  discipline as D13/D14/D22/D23, not a re-spend of API credits for a schema-only field.

**The first live run of `scripts/demo_agent_capability_interface.py` found a real bug
immediately**: asked Claude "What's the current balance for member 23456?", it had
`lookup_member_balance` available with `member_id` clearly declared as a required parameter — and
declined to call it, reasoning the tool was "specifically configured for member 12345" (the
literal, historical discovery-goal text used verbatim as the tool's description). A description
written for human provenance review reads, to an LLM choosing whether to call a tool, like a
hardcoded constraint.

Fixed by reusing existing logic, not writing new detection: `catalog.py::_generalize_description`
runs the exact same `_MEMBER_ID_RE` regex `agent/recorder.py` already uses to find the
parameterized ID in a goal, and rewrites "member 12345" to "a member (member_id)" — only when
`member_id` is actually a declared input, so a capability that happens to mention a member ID for
an unrelated reason isn't silently mangled. Re-ran the exact same live demo after the fix: Claude
correctly called `lookup_member_balance({"member_id": "23456"})`, the real deterministic replay
engine returned `$5.02` (member 23456's actual seeded balance, verified against `app/models.py`),
and Claude's final answer was correct. Both the broken-first and fixed-second runs' full
transcripts are saved for real in `/evidence/` — the bug wasn't fixed then quietly erased.

10 new offline tests (`tests/test_agent_interface.py`), including regression tests for both the
`confirm`-never-exposed safety property and the description-generalization fix. Full suite:
129/129 tests pass (up from 119).

## D28 — 2026-08-20 — Full-codebase review: a real crash-risk bug, found by inspection, not a
live failure

User asked for a full pass over everything — code clarity included, not just re-verifying prior
work. `pyflakes` across every module first: three trivial findings (two `f"..."` strings with no
placeholders, one unused import), fixed. The real find came from actually reading
`agent/tools.py` line by line rather than trusting its own comments.

`execute_click`, `execute_type`, and `execute_navigate` each only caught
`playwright.sync_api.TimeoutError`. Verified directly against a real Playwright page: calling
`.fill()` on a `<select>` element (role "combobox") raises a plain `Error` immediately — "Element
is not an `<input>`, `<textarea>` or `[contenteditable]` element" — never a timeout. That plain
`Error` is *not* caught by `except PlaywrightTimeoutError`, so `execute_type`'s own
`select_option` fallback (there specifically to handle `<select>` elements) was dead code, and —
worse — the uncaught exception would propagate straight out of `run_discovery()`'s loop, which
only catches `ToolExecutionError` at its outer boundary, crashing the entire discovery run instead
of surfacing a recoverable tool error the model could reason about. Same narrow-except shape in
`execute_click`/`execute_navigate` meant *any* non-timeout Playwright error there (element
detached, not visible, navigation aborted, ...) had the identical crash risk — this wasn't a
select-specific bug, it was a systemic one.

Confirmed `TimeoutError` is a subclass of Playwright's own `Error` (`issubclass(TimeoutError,
Error) == True`), so the fix is a strict broadening, not a behavior change for the timeout case:
all three functions now catch `PlaywrightError`, always re-raised as `ToolExecutionError` so
`run_discovery()`'s existing handling (log as `tool_error`, let the model see the failure message
and decide what to do next) actually applies. `execute_type`'s select fallback also now tries
`select_option(value=text, ...)` then `select_option(label=text, ...)` — belt-and-suspenders,
since a live check showed Playwright's `value=` parameter already resolves against label text too
when it doesn't match a value attribute, not strictly the HTML `value`.

**This one really mattered for a claim already in `REPORT.md`.** The Cuts section said the action
vocabulary has "no `select`-dropdown... primitive," pointing at `open_subaccount`'s account-type
field as the reason it's always left at its default. That framing was wrong — a fallback for
exactly this case already existed, it just couldn't ever run. Verified live after the fix: a goal
requiring `open_subaccount`'s non-default "Vacation Club" option (`page.locator("body")
.aria_snapshot()` confirms the model sees the full option list, not just the current selection) —
the model correctly typed "Vacation Club" into the combobox, and the escalation reason text on the
resulting confirmation page read back "...opening a new Vacation Club sub-account..." confirming
the real DOM selection changed, not just the model's belief about it. (That particular run hit
`max_steps` before a final `finish()` — an unrelated step-budget issue from an earlier dead-end
detour in the same run, not a sign the select mechanism itself failed — the tier log shows the
combobox interaction succeeded at steps s6-s9.) `REPORT.md`'s Cuts section corrected to describe
what's actually true. Full suite: 129/129 (no new tests needed — this was an exception-handling
correctness fix in already-tested code paths, verified by direct interpreter checks and one real
live run, not new unit coverage).

## D29 — 2026-08-20 — dispute_transaction and update_member_address were silently missing the
exact business-outcome pattern D14 already established

Same full-codebase review pass as D28, continued past `agent/tools.py` into `agent/compiler.py`.
`_KNOWN_OUTCOMES` has real, declared `MEMBER_NOT_FOUND`/`PERMISSION_DENIED` entries for
`lookup_member_balance`, `open_subaccount`, and `lookup_latest_transaction` — but nothing for
`dispute_transaction` or `update_member_address`, the two capabilities added later (D20/D23) to
prove discovery generalizes to genuinely new features. Checked live rather than assumed: replayed
both against a locked member (`99999`) and a not-found member (`00000`), with `--confirm` since
both are `risk_level=risky`.

Both reported `status=hard_failure` for what is actually a real, expected condition — the exact
failure mode the assignment names as "the most common design mistake here," just the inverse
direction of the usual example (a legitimate business outcome misreported as a system failure,
not a failure misreported as success). Root cause was identical to D14: `member_detail.html`
never renders the "View Transactions" / "Update Mailing Address" link at all for a locked member
(only the `msg-denied` branch renders), so replay's locator resolution correctly finds nothing —
but with no declared `expected_outcomes` on that step to check against, `_check_expected_outcomes`
had nothing to match and fell through to `hard_failure`.

Fixed by adding the same two-rule pattern (`MEMBER_NOT_FOUND` on the "View" click,
`PERMISSION_DENIED` on the capability's own next link) already proven for the other three
capabilities — not new logic, the same `_KNOWN_OUTCOMES`/`_attach_expected_outcomes` mechanism,
just extended to cover the two capabilities that had been missed. Patched both already-compiled
artifacts by calling the real `_attach_expected_outcomes` function directly against their loaded
`Capability.steps` (not hand-edited JSON), round-trip validated, then re-verified live: both
replays now correctly return `status=business_outcome` with the right code. 2 new regression
tests (`tests/test_compiler.py`). Full suite: 131/131 (up from 129).

## D30 — 2026-08-20 — The dead-end escalation path discarded whatever the human actually typed

Found by a user genuinely playing the operator role: their own discovery run hit a dead-end
escalation (`--open-console-on-escalation` wasn't used that run, so no auto-opened console), and
rather than wait, they manually clicked "Confirm and Open Account" directly in the live browser
window. I'd already warned against this earlier in the session — the discovery process isn't
watching the page, only the lease file, so a manual click doesn't unblock it, and worse, the
model's next observation won't match whatever it last expected. Exactly that happened: the model
resumed, saw a completed-order confirmation page instead of the form it left off on, and
(correctly, per its own system-prompt rules about unexpected state) escalated a second time,
narrating its best guess at what happened — not a hallucination exactly, more a reasonable
inference forced by having zero information about the actual cause, since no human note ever
reached it.

That gap was real and worth fixing regardless of the manual-click detour that surfaced it:
`agent/discovery.py`'s dead-end branch called `trigger_escalation(...)` and threw away its return
value entirely — the `escalate()` tool-call path already threads `lease.context.get("decision")`
and `human_actions_summary` back to the model on resume (D12), but the dead-end path never did.
A human typing a real note while resolving a dead-end — "I clicked X for you, carry on," "don't
try that path again" — silently went nowhere; the model was told only "re-observe" with zero
context for why the page might look different than expected.

Fixed to match the same pattern D12 established: read `lease.context.get("human_actions_summary",
"")` after resume and fold it into `last_action_result`, so it's part of what the model actually
sees on its next turn (a dead-end has no natural "approved/declined" concept, since nothing was
being asked permission for — only the note itself is threaded through, not a decision). New smoke
test `scripts/smoke_test_dead_end_human_note.py` (same real-browser-plus-scripted-LLM pattern as
`smoke_test_escalation_timeout.py`): scripts 4 identical `type` calls so the search box's value
stops changing after the first, triggering a real dead-end by the 4th observation; a background
thread resumes with a real note the instant the lease flips to human; asserts the note appears in
`escalation_resumed`'s logged context AND in the next turn's actual `last_action_result` — not
just internally logged but genuinely reaching the model. All 4 checks pass live. Full suite:
131/131 (smoke test, not a pytest unit test — matches this repo's existing split between fast
offline unit tests and real-browser smoke tests for discovery-loop mechanics, D8).

## D31 — 2026-08-20 — On-page pause banner: a human watching the automation's own window had no
visual signal it was paused

User's suggestion, watching their own successful `--open-console-on-escalation` run: the operator
console makes escalation state obvious to whoever's looking *there*, but a human watching the
automation's own live browser window (the one being handed over) had nothing — no banner, no
visual cue — only the terminal said "ESCALATED." A real gap in the handoff experience, not a bug.

Added `escalation/controller.py::_inject_pause_banner` — a small red fixed banner
("⏸ PAUSED — awaiting human approval at http://localhost:5001 — <reason>"), injected client-side
via `page.evaluate()` right before the evidence screenshot is taken, so both the live window AND
every escalation screenshot going forward show it. The mock app needs zero knowledge of this —
it's pure escalation-layer injection, same separation of concerns as everything else here.

Two things had to be right, not just "does a banner appear":
1. **`aria-hidden="true"`** — verified live against a real page that the banner's text does not
   leak into `agent/perception.py::build_observation`'s accessibility tree. A banner the *model*
   itself could perceive as page content would be a real regression, not a UX nicety.
2. **Removed on resume** (`_remove_pause_banner`, called right before `trigger_escalation`
   returns) — verified live it's actually gone from the DOM afterward, not just visually
   overwritten by a subsequent navigation.

Verified live end-to-end against the real app (banner present in DOM, absent from accessibility
tree, present in a real screenshot, gone after removal) before writing any tests. 4 new tests in
`tests/test_escalation.py`, including one that a page unevaluable mid-navigation never breaks the
real escalation over a cosmetic banner (best-effort, wrapped in `except Exception: pass`, same
posture as the existing screenshot capture). Full suite: 134/134 (up from 131).

## D32 — 2026-08-20 — A full outcome-matrix re-sweep (prompted by "be ready to defend every
decision") found stale-artifact drift, and a real bug in the patch mechanism itself

Re-reading the assignment's evaluation stance — "be ready to defend every decision," the artifact
schema and deterministic replay are named load-bearing — the honest response wasn't to assume
everything already verified still holds after 31 rounds of changes stacked on top of each other.
Re-ran the full success/`MEMBER_NOT_FOUND`/`PERMISSION_DENIED` (+`NO_TRANSACTIONS` where it
applies) matrix live against all 5 real capabilities, fresh, right now.

Found: `lookup_latest_transaction` replayed against a locked member (`99999`) returned
`hard_failure`, not the expected `PERMISSION_DENIED` — even though `agent/compiler.py`'s
`_KNOWN_OUTCOMES` has declared `MEMBER_NOT_FOUND`/`PERMISSION_DENIED` for this exact capability
since before this session started. The *compiler* was correct; the *already-compiled artifact*
wasn't — checked directly, `capabilities/lookup_latest_transaction.v1.json`'s steps s4/s5 had
empty `expected_outcomes`, meaning some earlier manual patch (D22's, most likely, which predates
these two rules being added) never got re-applied after the rules changed. Same class of gap as
D29, just on the one capability D29's sweep didn't happen to touch.

Fixing it the established way — reload the artifact, call `_attach_expected_outcomes` again,
re-save — surfaced a second, real bug: the function isn't idempotent. `outcomes = list(
step.expected_outcomes)` starts from whatever the step already has, and every matching rule gets
appended unconditionally with no check for "is this exact rule already here" — so re-running it
against `lookup_latest_transaction` duplicated the `NO_TRANSACTIONS` outcome already correctly
present on the extract step (from the original, correct compile). Re-running a compile-time
function against an already-compiled artifact is the *exact, established* pattern this whole
build uses to patch artifacts after a rule changes (D13/D14/D22/D23/D29) — it has to be safe to
call twice, and it wasn't.

Fixed `_attach_expected_outcomes` to track `(condition, code)` pairs already present per step and
skip appending a duplicate — confirmed live that running it twice against the same steps now
produces an identical result. Rebuilt `capabilities/lookup_latest_transaction.v1.json` from the
last clean committed version (not the already-duplicated one) using the fixed function, round-trip
validated, then re-verified live: the locked-member replay now correctly returns
`business_outcome`/`PERMISSION_DENIED`. Re-swept all 5 capabilities' full outcome matrices after
the fix — every one now correct. 1 new regression test (`tests/test_compiler.py`, asserts running
the function twice yields the same result). Full suite: 135/135 (up from 134).
