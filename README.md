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

- **Offline (no browser, no API key):** `pytest tests/` — 91 unit tests covering the schema,
  guardrails, perception parsing, the recorder's 3-tier locator logic, the compiler, the replay
  engine's pure helpers, and the escalation lease mechanism, all against fixtures or fake
  Playwright-shaped stand-ins. Runs in under 2 seconds, no network.
- **Needs a real browser, no API key:** `scripts/verify_perception_live.py`,
  `scripts/smoke_test_discovery.py` (scripted fake LLM), `scripts/smoke_test_replay.py`,
  `scripts/smoke_test_escalation_timeout.py` (regression test for a real timing bug — see
  `DECISIONS.md` D16), `scripts/smoke_test_operator_auth.py` (live integration test for the
  operator console's authentication — see `DECISIONS.md` D18). These exist specifically to
  validate mechanics without spending API credits — see `DECISIONS.md` D8.
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

### Forcing a human escalation by hand

```bash
# Terminal A: the operator console
source .venv/bin/activate
set -a; source .env; set +a   # loads OPERATOR_USERNAME/OPERATOR_PASSWORD if you set them
python3 escalation/operator_page.py    # http://localhost:5001

# Terminal B: a goal likely to hit the dead-end detector or need risky-action confirmation
python3 scripts/run_discovery.py --goal "..." --target "http://localhost:5050/search"
```

When the run escalates, open `http://localhost:5001` — your browser will prompt for a
username/password (HTTP Basic Auth, per D18); use whatever `OPERATOR_USERNAME`/`OPERATOR_PASSWORD`
you set, or the one-time credential Terminal A printed at startup if you didn't set one. Then
you'll see the reason, the current URL, and a screenshot of the live session, with Approve /
Decline / plain-Resume buttons. See
`scripts/demo_escalation.py` for a fully automated version of this same sequence (real browser,
real separate operator process, real HTTP calls) used to produce
`evidence/escalation_demo_sequence.json`.

## 4. Evidence

`/evidence/` holds the real artifacts from the runs described in `DECISIONS.md` — discovery
transcripts, compiled capabilities' replay results (success / both business outcomes / an
injected hard failure, for both capabilities), the escalation demo sequence with its screenshot,
and a captured guardrail-violation transcript. Nothing in it is synthesized after the fact;
every file is what the corresponding script actually wrote when it ran.

## 5. Project layout

```
app/            mock legacy core-banking Flask/SQLite app (Phase 0)
agent/          perception, discovery loop, recorder, compiler (Phases 1-4)
artifact/       the Capability/Step/Result Pydantic schema (the artifact contract)
replay/         the deterministic replay engine (Phase 5)
guardrails/     allowlist enforcement + redaction (Phase 6)
escalation/     lease-based human handoff + operator console (Phase 7)
capabilities/   compiled capability artifacts (the deliverable output)
scripts/        CLI entrypoints + smoke tests
tests/          91 offline unit tests
evidence/       real run output (see above)
```
