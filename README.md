# Computer-Use Automation System

A small, real end-to-end version of interface.ai's "hands for AI agents" system: an LLM drives a
live legacy banking web app to accomplish a goal, the successful run is compiled into a typed,
versioned, reusable **capability** artifact, and that artifact is replayed **deterministically**
— no LLM in the loop — with real runtime-error and business-outcome handling, safety guardrails,
and a human-in-the-loop escalation/handoff path.

See [`REPORT.md`](REPORT.md) for the design write-up and [`DECISIONS.md`](DECISIONS.md) for a
running log of every non-obvious decision (including several real bugs found while building this,
with what broke and how they were fixed).

## 1. Setup

Requires Python 3.11+ (built and run on 3.14) and a real Anthropic API key.

```bash
git clone <this-repo-url>
cd Interface_Bank_AI

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium      # downloads a real Chromium binary, no root needed

echo "ANTHROPIC_API_KEY=sk-ant-..." > .env   # gitignored, never committed
```

**Operator console credentials (D18):** `escalation/operator_page.py` requires HTTP Basic Auth —
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
throughout this repo instead. See `DECISIONS.md` D2.

**A note on conda:** if you have conda/Anaconda installed and your shell auto-activates a `(base)`
environment, `source .venv/bin/activate` can silently fail to actually put this project's `.venv`
first on `PATH`, and `python3 scripts/...` will run against `(base)`'s Python instead — which
doesn't have Playwright installed, so you'll hit `ModuleNotFoundError: No module named
'playwright'`. Check `which python3` after activating; it should print a path ending in
`.venv/bin/python3`. If it doesn't, run `conda deactivate` first, then `source .venv/bin/activate`
again.

## 2. Running without live services

The parts that need a real browser and/or a real LLM:

- **Offline (no browser, no API key):** `pytest tests/` — 134 unit tests covering the schema,
  guardrails, perception parsing, the recorder's 3-/4-tier locator logic, the compiler, the replay
  engine's pure helpers, the escalation lease mechanism, the CLI's pure helper logic (default
  checkpoint, risk-level inference, the auto-open-console watcher), and the agent-facing
  capability catalog/invocation routing, all against fixtures or fake Playwright-shaped
  stand-ins. Runs in under 2 seconds, no network.
- **Needs a real browser, no API key:** `scripts/verify_perception_live.py`,
  `scripts/smoke_test_discovery.py` (scripted fake LLM), `scripts/smoke_test_replay.py`,
  `scripts/smoke_test_escalation_timeout.py` (regression test for a real timing bug — see
  `DECISIONS.md` D16), `scripts/smoke_test_operator_auth.py` (live integration test for the
  operator console's authentication — see `DECISIONS.md` D18), `scripts/smoke_test_dead_end_human_note.py`
  (regression test for a human's resume note reaching the model — see `DECISIONS.md` D30). These
  exist specifically to validate mechanics without spending API credits — see `DECISIONS.md` D8.
- **No browser, no API key:** `python3 scripts/demo_encryption_at_rest.py` proves the
  encryption-at-rest module (`guardrails/encryption.py`, D19) works end to end against a real
  file on disk — generates a throwaway key if `EVIDENCE_ENCRYPTION_KEY` isn't set in `.env`.
- **Needs a real browser AND a real API key:** `scripts/run_discovery.py` and anything under
  "demo path" below. This is the one part of the system that has to be real — see `REPORT.md`.

## 3. Demo path

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

### Forcing a human escalation by hand

This is the interactive version — you play the banker who approves or declines a real pending
request, rather than letting `--auto-approve-escalation` do it unattended:

```bash
# Terminal A: the operator console
source .venv/bin/activate
set -a; source .env; set +a   # loads OPERATOR_USERNAME/OPERATOR_PASSWORD if you set them
python3 escalation/operator_page.py    # http://localhost:5001, prints a one-time
                                        # credential to the console if you didn't set one

# Terminal B: an irreversible goal, WITHOUT --auto-approve-escalation, so it actually blocks
python3 scripts/run_discovery.py \
  --goal "Open a new Christmas Club sub-account for member 34567 with a \$50 opening deposit, and complete the account creation." \
  --target "http://localhost:5050/search" \
  --capability-id open_subaccount --max-steps 12 --headless
```

When the run escalates, open `http://localhost:5001` — your browser will prompt for a
username/password (HTTP Basic Auth, per D18); use whatever `OPERATOR_USERNAME`/`OPERATOR_PASSWORD`
you set, or the one-time credential Terminal A printed at startup if you didn't set one. Then
you'll see the reason, the current URL, and a screenshot of the live session, with Approve /
Decline / plain-Resume buttons. See
`scripts/demo_escalation.py` for a fully automated version of this same sequence (real browser,
real separate operator process, real HTTP calls) used to produce
`evidence/escalation_demo_sequence.json`.

### Teach it something it's never seen

`capabilities/` currently has 5 files — the assignment requires 2. Two of the extra three
(`dispute_transaction`, `update_member_address`) exist specifically because this exact "make it
learn something new" question came up during review, and both were proven live rather than just
described: point discovery at a real app feature with **zero** prior capability, on a goal never
seen before, and watch it build one from scratch (`DECISIONS.md` D23 has the full write-up of both
runs, including a real bug the first one surfaced).

To get a genuinely blank slate yourself — not just a capability_id you personally haven't typed
yet — delete its compiled artifact first, then discover it fresh:

```bash
rm capabilities/dispute_transaction.v1.json    # or update_member_address.v1.json

python3 scripts/run_discovery.py \
  --goal "File a dispute for member 23456's most recent transaction, reason 'duplicate charge'." \
  --target "http://localhost:5050/search" \
  --capability-id dispute_transaction --headless
```

Two things worth watching for, both real and both verified live twice now (once per feature,
`DECISIONS.md` D23):

1. **It may escalate on its own.** Submitting a form that changes a real record is exactly the
   "state-changing, hard-to-reverse action" the system prompt tells the model to stop and confirm
   before taking — both `dispute_transaction` and `update_member_address` did, unprompted, on
   their first-ever run. If it does, follow the "Forcing a human escalation by hand" steps above
   to approve or decline it — no `--auto-approve-escalation` needed if you want to do that part
   yourself.
2. **A capability discovered this way that *did* escalate gets compiled `risk_level: risky`
   automatically** (D23's `_infer_risk_level` — no capability needs to be hand-listed for this),
   so replaying it back will refuse without `--confirm`, same as `open_subaccount`.

One honest limit to know before replaying `update_member_address`: parameter detection only
generalizes the `member_id` (D13) — replaying it against a different member re-targets *that*
member correctly, but writes back the same address values recorded during discovery, not new
ones. It's "apply this recorded change to someone else," not "make up a new value per member."

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
  is Christmas Club). This now works (D28) — the `type` tool falls back to `select_option` for a
  `<select>` element, and the model can see every option's label via the accessibility tree, not
  just the current selection.

## 4. Evidence

`/evidence/` holds the real artifacts from every run described in `DECISIONS.md` — 17 discovery
transcripts, 22 replay results (success / business outcomes / an injected hard failure, across
all 5 capabilities), 13 real escalations with screenshots, the fully-automated escalation demo
sequence, a captured guardrail-violation transcript, and two real Claude tool-use transcripts from
the agent-facing capability interface (§6). Nothing in it is synthesized after the fact; every
file is what the corresponding script actually wrote when it ran.
**`evidence/README.md`** is a short curated index — start there rather than the raw file list if
you want one traceable discovery → artifact → replay example plus one of each exceptional-state
replay, without reading all 77 files.

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
tests/            134 offline unit tests
evidence/         real run output (see above)
```

## 6. Stretch goal: agent-facing capability interface

`capabilities/*.json` exposed as a catalog an AI agent can discover and invoke by name with typed
args — full design reasoning in `REPORT.md` §8, full write-up (including a real bug the first
live run found) in `DECISIONS.md` D27.

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
