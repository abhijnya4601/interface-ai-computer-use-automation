# Computer-Use Automation System

A small, real end-to-end version of interface.ai's "hands for AI agents" system: an LLM drives a
live legacy banking web app to accomplish a goal, the successful run is compiled into a typed,
versioned, reusable **capability** artifact, and that artifact is replayed **deterministically**
— no LLM in the loop — with real runtime-error and business-outcome handling, safety guardrails,
and a human-in-the-loop escalation/handoff path.

See [`REPORT.md`](REPORT.md) for the design write-up, including the trade-offs made and several
real bugs found while building this — what broke and how they were fixed.

## 1. Setup

Requires Python 3.11+ (built and run on 3.14) and a real Anthropic API key.

```bash
git clone https://github.com/abhijnya4601/interface-ai-computer-use-automation.git
cd interface-ai-computer-use-automation

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium      # downloads a real Chromium binary, no root needed

echo "ANTHROPIC_API_KEY=sk-ant-..." > .env   # gitignored, never committed
```

**Operator console credentials:** `escalation/operator_page.py` requires HTTP Basic Auth —
whoever can reach it can approve an irreversible financial action, so it never serves
unauthenticated. Set a stable credential in `.env`:

```bash
echo "OPERATOR_USERNAME=banker" >> .env
echo "OPERATOR_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')" >> .env
```

If you skip this, the console generates and prints a one-time random password to its own
terminal at startup instead of ever running open — it never silently serves without auth.

**A note on port 5000:** the mock app runs on **5050**, not 5000. macOS's built-in AirPlay
Receiver squats on port 5000 and answers HTTP requests before Flask ever sees them — every
`localhost:5000` reference you might expect from a typical Flask tutorial is `localhost:5050`
throughout this repo instead.

**A note on conda:** if you have conda/Anaconda installed and your shell auto-activates a `(base)`
environment, `source .venv/bin/activate` can silently fail to actually put this project's `.venv`
first on `PATH`, and `python3 scripts/...` will run against `(base)`'s Python instead — which
doesn't have Playwright installed, so you'll hit `ModuleNotFoundError: No module named
'playwright'`. Check `which python3` after activating; it should print a path ending in
`.venv/bin/python3`. If it doesn't, run `conda deactivate` first, then `source .venv/bin/activate`
again.

## 2. Running without live services

The parts that need a real browser and/or a real LLM:

- **Offline (no browser, no API key):** `pytest tests/` — 135 unit tests covering the schema,
  guardrails, perception parsing, the recorder's 3-/4-tier locator logic, the compiler, the replay
  engine's pure helpers, the escalation lease mechanism, the CLI's pure helper logic (default
  checkpoint, risk-level inference, the auto-open-console watcher), and the agent-facing
  capability catalog/invocation routing, all against fixtures or fake Playwright-shaped
  stand-ins. Runs in under 2 seconds, no network.
- **Needs a real browser, no API key:** `scripts/verify_perception_live.py`,
  `scripts/smoke_test_discovery.py` (scripted fake LLM), `scripts/smoke_test_replay.py`,
  `scripts/smoke_test_escalation_timeout.py` (regression test for a real timing bug — a
  human's escalation-review time was being counted against the run's own wall-clock budget),
  `scripts/smoke_test_operator_auth.py` (live integration test for the operator console's
  authentication), `scripts/smoke_test_dead_end_human_note.py` (regression test for a human's
  resume note reaching the model on a dead-end resume). These exist specifically to validate
  mechanics without spending API credits.
- **No browser, no API key:** `python3 scripts/demo_encryption_at_rest.py` proves the
  encryption-at-rest module (`guardrails/encryption.py`) works end to end against a real
  file on disk — generates a throwaway key if `EVIDENCE_ENCRYPTION_KEY` isn't set in `.env`.
- **Needs a real browser AND a real API key:** `scripts/run_discovery.py` and anything under
  "demo path" below. This is the one part of the system that has to be real — see `REPORT.md`.

## 3. Demo path

**Headless vs. headed — what you'll actually see.** Every `run_discovery.py` command below
includes `--headless`, meaning no browser window opens — the agent runs invisibly and you only
see status printed to your terminal. **Drop `--headless` from any command if you want to watch
the real Chromium window do the work live** — that's the more interesting way to first see this
run. `run_replay.py` is headless by default too; add `--headed` to it if you want to watch a
replay instead of just reading its printed result.

**Handling escalation — three ways, pick one per run:**
1. **`--auto-approve-escalation`** — fully unattended; a background process approves it for you
   after a short delay. Used below wherever a command needs to complete without you (e.g.
   recording the risky capability once, non-interactively).
2. **`--open-console-on-escalation`** (recommended for trying this yourself) — starts the
   operator console for you and opens it in your browser the instant the run actually escalates,
   so you can click Approve/Decline live without hunting for a port or missing the moment. This
   is what "Testing human-in-the-loop escalation yourself" below uses.
3. **Neither flag** — the run blocks and waits. Start `escalation/operator_page.py` yourself in
   a separate terminal beforehand if you want to approve it manually without the auto-open.

**Terminal 1 — start the mock bank app:**

```bash
source .venv/bin/activate
cd app
python3 -c "import models; models.init_db(); models.seed()"
python3 app.py    # http://localhost:5050
```

**Terminal 2 — run the agent on a goal, for real:**

```bash
source .venv/bin/activate
set -a; source .env; set +a   # loads ANTHROPIC_API_KEY

python3 scripts/run_discovery.py \
  --goal "Look up member 12345 and read their current savings balance." \
  --target "http://localhost:5050/search" \
  --capability-id lookup_member_balance \
  --headless
```

This launches a real (persistent) Chromium context, runs the real observe→decide→act loop
against Claude, and on success:
- saves the full structured transcript to `evidence/discovery_<run_id>.jsonl`
- compiles and saves `capabilities/lookup_member_balance.v1.json`

**Then replay the resulting artifact — no LLM involved:**

```bash
# a NEW member_id, never seen during discovery -- proves real parameterization
python3 scripts/run_replay.py \
  --capability capabilities/lookup_member_balance.v1.json \
  --params '{"member_id": "23456"}'

# a business outcome, not a crash
python3 scripts/run_replay.py \
  --capability capabilities/lookup_member_balance.v1.json \
  --params '{"member_id": "88888"}'   # not seeded -> MEMBER_NOT_FOUND

python3 scripts/run_replay.py \
  --capability capabilities/lookup_member_balance.v1.json \
  --params '{"member_id": "99999"}'   # locked -> PERMISSION_DENIED
```

Each replay prints and saves a structured `Result` (`status`, `outputs`,
`business_outcome_code`, `failure_detail`) to `evidence/replay_*.json`.

### Running a different task entirely

Every command above follows one fixed shape — nothing here is hardcoded to these specific
examples, so swap in whatever you actually want the agent to try:

```bash
python3 scripts/run_discovery.py \
  --goal "<a real, plain-English instruction — this is the only thing the model reads>" \
  --target "http://localhost:5050/search" \
  --capability-id <a_short_name_for_this_task> \
  --headless
```

- **`--goal`** isn't matched against a fixed list or a menu of known intents — it's the literal
  text the model reasons over each turn, so a genuinely different goal produces genuinely
  different behavior. This is what "Teach it something it's never seen" below actually
  demonstrates: two goals that were never scripted in advance, run for the first time, live.
- **`--capability-id`** just names the output file (`capabilities/<capability-id>.v1.json`) — pick
  anything unused and it won't overwrite one of the 5 real capabilities already in this repo.
- **`--target`** stays `http://localhost:5050/search` unless you've pointed the mock app at a
  different entry route yourself.
- Drop `--headless` if you want to watch the browser; add `--open-console-on-escalation` if the
  task might need a state-changing action confirmed (see "Testing human-in-the-loop escalation yourself"
  below for what that looks like end to end).

Whatever gets compiled replays exactly like any other capability:

```bash
python3 scripts/run_replay.py \
  --capability capabilities/<your-capability-id>.v1.json \
  --params '{"member_id": "<any seeded or unseeded ID>"}'
```

### The second, risky capability

`open_subaccount` is state-mutating (creates a real DB row) and `risk_level: risky`. Recording
it end-to-end (including the actual irreversible submit) requires either an interactive operator
session, or `--auto-approve-escalation` to drive the real escalation/operator-console handoff
unattended:

```bash
python3 scripts/run_discovery.py \
  --goal "Open a new Christmas Club sub-account for member 12345 with a \$50 opening deposit, and complete the account creation." \
  --target "http://localhost:5050/search" \
  --capability-id open_subaccount \
  --max-steps 12 --auto-approve-escalation --headless
```

Replaying it requires explicit confirmation — without `--confirm` it's rejected before touching
the page:

```bash
python3 scripts/run_replay.py \
  --capability capabilities/open_subaccount.v1.json \
  --params '{"member_id": "23456"}'              # -> hard_failure, confirm=True required

python3 scripts/run_replay.py \
  --capability capabilities/open_subaccount.v1.json \
  --params '{"member_id": "23456"}' --confirm     # -> success, real DB row created
```

### Trying the third capability: `lookup_latest_transaction`

Same discover-then-replay shape, different member IDs surface a real business outcome instead of
a fixed example:

```bash
python3 scripts/run_discovery.py \
  --goal "Find the most recent transaction date for member 12345." \
  --target "http://localhost:5050/search" \
  --capability-id lookup_latest_transaction --headless

python3 scripts/run_replay.py \
  --capability capabilities/lookup_latest_transaction.v1.json \
  --params '{"member_id": "23456"}'   # has transactions -> success

python3 scripts/run_replay.py \
  --capability capabilities/lookup_latest_transaction.v1.json \
  --params '{"member_id": "34567"}'   # empty history -> NO_TRANSACTIONS (business outcome)
```

### Testing human-in-the-loop escalation yourself

**Why this happens at all — the model decides, a human doesn't force it.** Nothing in this repo
maintains a fixed list of "risky goals that need a human." The agent's own system prompt
(`agent/discovery.py::_system_prompt`) tells it: if completing the goal requires a state-changing,
hard-to-reverse action, stop and call `escalate` instead of taking that step yourself. Whether any
given goal actually triggers this is the model's live judgment call, made fresh each run against
what it's about to do — not something decided in advance by a human or a config file. A human's
only role is what happens *after* that: reviewing the specific pending action and deciding
Approve or Decline. (`risk_level: risky` on a compiled capability is inferred *afterward*, from
whether this happened during discovery (`_infer_risk_level`) — never the other way around.)

To watch this yourself, in one terminal, with the console opening automatically:

```bash
source .venv/bin/activate
set -a; source .env; set +a

python3 scripts/run_discovery.py \
  --goal "Open a new Christmas Club sub-account for member 34567 with a \$50 opening deposit, and complete the account creation." \
  --target "http://localhost:5050/search" \
  --capability-id open_subaccount --max-steps 12 --open-console-on-escalation
```

No `--headless` here on purpose — watch the real browser window reach the confirmation page, then
watch your own browser pop open the operator console the moment it actually escalates. Log in
with the credentials printed in your terminal (or your own `OPERATOR_USERNAME`/`OPERATOR_PASSWORD`
if you set them), read the real reason and screenshot, and click Approve or Decline. Prefer two
terminals and starting the console yourself instead? Drop `--open-console-on-escalation` and run
`python3 escalation/operator_page.py` in a separate terminal first, then open
`http://localhost:5001` by hand once it escalates.

See `scripts/demo_escalation.py` for a fully automated version of this same sequence (real
browser, real separate operator process, real HTTP calls, zero manual clicking) used to produce
`evidence/escalation_demo_sequence.json`.

### Teach it something it's never seen

`capabilities/` currently has 5 files — the assignment requires 2. Two of the extra three
(`dispute_transaction`, `update_member_address`) exist specifically because this exact "make it
learn something new" question came up during review, and both were proven live rather than just
described: point discovery at a real app feature with **zero** prior capability, on a goal never
seen before, and watch it build one from scratch — both runs escalated on the model's own
judgment and surfaced a real bug along the way (see `REPORT.md` §3).

To get a genuinely blank slate yourself — not just a capability_id you personally haven't typed
yet — delete its compiled artifact first, then discover it fresh:

```bash
rm capabilities/dispute_transaction.v1.json    # or update_member_address.v1.json

python3 scripts/run_discovery.py \
  --goal "File a dispute for member 23456's most recent transaction, reason 'duplicate charge'." \
  --target "http://localhost:5050/search" \
  --capability-id dispute_transaction --headless
```

Two things worth watching for, both real and both verified live twice now (once per feature):

1. **It may escalate on its own.** Submitting a form that changes a real record is exactly the
   "state-changing, hard-to-reverse action" the system prompt tells the model to stop and confirm
   before taking — both `dispute_transaction` and `update_member_address` did, unprompted, on
   their first-ever run. If it does, follow the "Testing human-in-the-loop escalation yourself" steps above
   to approve or decline it — no `--auto-approve-escalation` needed if you want to do that part
   yourself.
2. **A capability discovered this way that *did* escalate gets compiled `risk_level: risky`
   automatically** (`_infer_risk_level` — no capability needs to be hand-listed for this), so
   replaying it back will refuse without `--confirm`, same as `open_subaccount`.

One honest limit to know before replaying `update_member_address`: parameter detection only
generalizes the `member_id` — replaying it against a different member re-targets *that* member
correctly, but writes back the same address values recorded during discovery, not new ones. It's
"apply this recorded change to someone else," not "make up a new value per member."

### A fuller menu — other goals worth trying, and what to expect

Not an exhaustive list — the point is the system takes arbitrary goal text, not a fixed menu — but
these cover genuinely different things to watch for, each verified live at least once:

- **A business outcome reached cold, no capability guiding it.** Point discovery straight at an
  edge-case member instead of the happy path, e.g. `--goal "Look up member 99999 and read their
  current savings balance."` with `--capability-id lookup_member_balance` (use a throwaway
  `--capability-id` if you don't want to touch the real file). The model has no declared
  `expected_outcomes` to lean on here — that mechanism is replay-only — so this is its own
  from-scratch reasoning: it reads the "restricted" page and correctly finishes with
  `status=business_outcome`, `business_outcome_code=PERMISSION_DENIED`, same as a not-found member
  ID produces `MEMBER_NOT_FOUND`.
- **A goal combining two existing capabilities' steps in one run** — e.g. "Look up member 12345's
  balance, then find their most recent transaction." Untested territory: the compiler declares
  business outcomes per-capability from a single `capability_id`, so a merged run may compile
  oddly or need two separate discovery calls. Worth trying specifically *because* it's untested.
- **A goal that needs a non-default `<select>` option** — e.g. "Open a Vacation Club sub-account
  for member 45678 with a $25 opening deposit, and complete the account creation." (the default
  is Christmas Club). This now works — the `type` tool falls back to `select_option` for a
  `<select>` element, and the model can see every option's label via the accessibility tree, not
  just the current selection.

### Everything the mock app can learn, in one place

Every goal type demonstrated above, gathered into one table — `--capability-id` is what to pass,
`safe` capabilities never need `--confirm` on replay, `risky` ones always do:

| What it does | `--capability-id` | Risk | Member IDs worth trying | What you'll see |
|---|---|---|---|---|
| Look up a savings balance | `lookup_member_balance` | safe | `12345`/`23456`/`34567`/`56789` (success), `99999` (locked), `00000` (not seeded) | `success`, `PERMISSION_DENIED`, `MEMBER_NOT_FOUND` |
| Open a new sub-account | `open_subaccount` | risky | any active member; try a non-default account type (Vacation Club, General Savings) | escalates before the final submit; `--confirm` required to replay |
| Find the most recent transaction | `lookup_latest_transaction` | safe | `12345`/`23456` (have history), `34567`/`45678`/`56789` (empty) | `success`, `NO_TRANSACTIONS` |
| File a transaction dispute | `dispute_transaction` | risky | any member with transaction history | escalates on its own; `rm` the `.json` first for a genuinely blank-slate test |
| Update a mailing address | `update_member_address` | risky | any member | escalates on its own; same blank-slate note as above |

**To repeat any of this cleanly**, reset the mock bank back to its original seed data — this
clears anything a previous run changed (a new sub-account, a disputed transaction, a changed
address) without needing to restart the Flask app itself:

```bash
cd app && python3 -c "import models; models.init_db(); models.seed()" && cd ..
```

Run this between attempts if you want every member ID above to behave exactly as described,
regardless of what you tried before.

## 4. Evidence

**Start with [`evidence/README.md`](evidence/README.md)** — a short curated index pointing at one
traceable discovery → artifact → replay example plus one of each exceptional-state replay, rather
than the raw 87-file list below.

`/evidence/` holds the real artifacts from every run made while building and testing this — 18
discovery transcripts, 27 replay results (success / business outcomes / an injected hard failure, across
all 5 capabilities), 14 real escalations with screenshots, the fully-automated escalation demo
sequence, a captured guardrail-violation transcript, and two real Claude tool-use transcripts from
the agent-facing capability interface (§6). Nothing in it is synthesized after the fact; every
file is what the corresponding script actually wrote when it ran.

## 5. Project layout

```
app/              mock legacy core-banking Flask/SQLite app (Phase 0)
agent/            perception, discovery loop, recorder, compiler (Phases 1-4)
artifact/         the Capability/Step/Result Pydantic schema (the artifact contract)
replay/           the deterministic replay engine (Phase 5)
guardrails/       allowlist enforcement + redaction (Phase 6)
escalation/       lease-based human handoff + operator console (Phase 7)
agent_interface/  capabilities exposed as an agent-callable tool catalog (stretch goal, §6)
capabilities/     compiled capability artifacts (the deliverable output)
scripts/          CLI entrypoints + smoke tests
tests/            135 offline unit tests
evidence/         real run output (see above)
```

## 6. Stretch goal: agent-facing capability interface

`capabilities/*.json` exposed as a catalog an AI agent can discover and invoke by name with typed
args — full design reasoning, including a real bug the first live run found, in `REPORT.md` §8.

```bash
# terminal 1 — the mock app, same as any other demo
cd app && python3 -c "import models; models.init_db(); models.seed()" && python3 app.py

# terminal 2 — a real Claude API call discovers the catalog and invokes a capability by name
source .venv/bin/activate
set -a; source .env; set +a
python3 scripts/demo_agent_capability_interface.py
```

Asks Claude "What's the current balance for member 23456?" with the tool catalog attached —
watch it choose `lookup_member_balance`, call it with `{"member_id": "23456"}`, get back a real
result from the deterministic replay engine (no LLM in that path), and answer correctly. Saves
the full transcript to `evidence/agent_capability_interface_demo_*.json`.
