# MERIDIAN CORE — live demo runbook

Everything to run the adaptation end to end on demo day, in order, with what each step proves
and what "working" looks like. Target: **`web-sample.interface-hiring.com`** (hosted, no login
needed from you). Design write-up: [`ADAPTATION.md`](ADAPTATION.md). Decision log:
`DECISIONS.md` D36–D45.

---

## 0. One-time setup

```bash
git clone https://github.com/abhijnya4601/interface-ai-computer-use-automation.git
cd interface-ai-computer-use-automation
git checkout adaptation-meridian-core

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Create `.env` (gitignored):

```bash
cat > .env <<'ENV'
ANTHROPIC_API_KEY=sk-ant-...

# MERIDIAN demo operators — public, no real data (brief §2)
MERIDIAN_OPERATOR=teller1
MERIDIAN_PASSWORD=password
MERIDIAN_BRANCH=MAIN-001
MERIDIAN_SUPERVISOR_OPERATOR=super1
MERIDIAN_SUPERVISOR_PASSWORD=password

# operator console (escalation approvals) — stable creds so you don't hunt for a generated one
OPERATOR_USERNAME=banker
OPERATOR_PASSWORD=demo-operator-pw
ENV
```

**Every terminal, first:**

```bash
cd interface-ai-computer-use-automation
source .venv/bin/activate
set -a; source .env; set +a
```

---

## 1. Pre-flight (30 seconds, do this before you present)

```bash
python -m pytest tests/ -q                                              # -> 194 passed
curl -sS -o /dev/null -w "%{http_code}\n" https://web-sample.interface-hiring.com/signon   # -> 200
python scripts/meridian_scout.py 100234 103001                          # note the share ids it prints
```

`meridian_scout.py` matters: the hosted app is **stateful in memory** and only resets on
redeploy. Repeated risky runs move balances and place holds, so it prints which shares are
usable *right now* — `from_share` (OPEN, >$0), `to_share` (any OPEN), `holdable` (still on the
Place Hold form). Use those ids in steps 5–6 and the chatbot.

---

## 2. The heart of it — one real LLM discovery run (~30–60s, spends a little API)

```bash
python scripts/run_discovery.py \
  --goal "Search for member 100987 by member number and click Select. Then extract the first SHARES/BALANCES row: Share ID as share_id, Type as share_type, Balance as balance, Status as status. Finish with all four in outputs." \
  --target https://web-sample.interface-hiring.com/members \
  --capability-id demo_lookup --headless
```

**Working =** `status=success` + `capability saved to capabilities/demo_lookup.v1.json` (it also
prints `transcript saved to evidence/discovery_run_<id>.jsonl`).
**Proves:** the model drives the real legacy UI (no clean DOM, no test IDs), figures out the
flow, and it's compiled into a typed artifact. Open that transcript — every line is a real
Claude tool call:

```bash
T=$(ls -t evidence/discovery_run_*.jsonl | head -1)
python -c "import json;[print(' ',e['name'],e['input']) for l in open('$T') for e in [json.loads(l)] if e['type']=='tool_call']"
```

---

## 3. Deterministic replay — same artifact, no LLM, a different member

```bash
python scripts/run_meridian.py --capability capabilities/demo_lookup.v1.json --params '{"member_id":"100234"}'
```

**Working =** `status: success` and `outputs` with **member 100234's own** share row (recorded
off 100987). **Proves:** genuine parameterisation + deterministic replay. The tier log prints
`labeled_field` for the search box and `table_position` for the cells — position-anchored, the
values are never in the locator.

You can also use the committed capability: `capabilities/meridian_check_member_balance.v1.json`
(from discovery `run_c612111ab2`).

---

## 4. Exceptional states — detected and reported, not crashes

```bash
for k in validation notfound permission timeout maintenance server; do
  printf "%-12s -> " "$k"
  python scripts/run_meridian.py --capability capabilities/meridian_check_member_balance.v1.json \
    --params '{"member_id":"100234"}' --inject $k --label demo_$k 2>&1 \
    | grep -E '^status:|^business_outcome_code:|^recovery:' | tr '\n' ' '; echo
done
python scripts/run_meridian.py --capability capabilities/meridian_check_member_balance.v1.json \
  --params '{"member_id":"999999"}' --label demo_notseeded 2>&1 | grep -E '^status:|^business_outcome_code:'
```

**Working =**

| trigger | expect |
|---|---|
| `validation` (400) | `business_outcome` · `VALIDATION_REJECTED` |
| `notfound` (404) | `business_outcome` · `RECORD_NOT_FOUND` |
| `permission` (403) | `business_outcome` · `PERMISSION_DENIED` |
| `timeout` (440) | `recoverable_handled` · `recovery: [... reauth_and_retry ... gave_up]` |
| `maintenance` (503) | `recoverable_handled` · `recovery: [... retry ... gave_up]` |
| `server` (500) | `hard_failure` · `SERVER_ERROR` |
| member 999999 | `business_outcome` · `MEMBER_NOT_FOUND` |

**Proves:** the 3-class taxonomy (business outcome vs recoverable vs hard failure), HTTP-status +
body-text classification, and that recovery is real and bounded — 503 is retried x3, 440 is
re-authenticated + retried, then both report cleanly because a *forced* inject never clears.
(A transient error-rate would `recover` and the run would continue.)

---

## 5. A risky capability — the confirmation gate

```bash
# fill FROM / TO from `meridian_scout.py 100234` output
FROM=100234-S0001 ; TO=100234-MMKT-3

python scripts/run_meridian.py --capability capabilities/meridian_funds_transfer.v1.json \
  --params "{\"member_id\":\"100234\",\"from_share\":\"$FROM\",\"to_share\":\"$TO\",\"amount\":\"1.00\",\"memo\":\"demo\"}"
# -> status: hard_failure   (confirm=True required — refused before touching the page)

python scripts/run_meridian.py --capability capabilities/meridian_funds_transfer.v1.json \
  --params "{\"member_id\":\"100234\",\"from_share\":\"$FROM\",\"to_share\":\"$TO\",\"amount\":\"1.00\",\"memo\":\"demo\"}" --confirm
# -> status: success   outputs: {'confirmation': 'CN4800xx'}   (real ledger post)
```

**Proves:** irreversible actions are gated; `--confirm` (a decision only trusted code makes) is
what commits.

---

## 6. Supervisor-gated capability

```bash
HOLD=$(python scripts/meridian_scout.py --json 100234 | python -c "import sys,json;print(json.load(sys.stdin)[0]['holdable'][0])")

# as a teller -> refused as a business outcome
python scripts/run_meridian.py --capability capabilities/meridian_place_hold.v1.json \
  --params "{\"member_id\":\"100234\",\"share\":\"$HOLD\",\"reason\":\"LEGAL\",\"notes\":\"demo\"}" --confirm --role teller
# -> status: business_outcome   PERMISSION_DENIED

# as a supervisor -> posts
python scripts/run_meridian.py --capability capabilities/meridian_place_hold.v1.json \
  --params "{\"member_id\":\"100234\",\"share\":\"$HOLD\",\"reason\":\"LEGAL\",\"notes\":\"demo\"}" --confirm --role supervisor
# -> status: success   outputs: {'confirmation': 'CN4800xx'}
```

**Proves:** `requires_role` — the session signs on with the right credential; a teller gets a
clean business outcome, a supervisor completes it.

---

## 7. API + dashboard (Terminal A)

```bash
python -m api
```

- Browser → **http://localhost:8000** — capability catalog (all 7 §2.1, `meridian_signon` shown
  as `(precondition)`) + run history (**discovery and replay**) with status badges. Click any
  run for inputs / structured outputs / evidence (screenshots inline).
- The operator console (http://localhost:5001) starts automatically on the first risky invoke.

**Invoke over HTTP (Terminal B):**

```bash
curl -sS -X POST http://localhost:8000/api/capabilities/meridian_check_member_balance/invoke \
  -H 'content-type: application/json' -d '{"args":{"member_id":"101555"}}' | python3 -m json.tool
```

**Working =** JSON with `"result": {"status": "success", "outputs": {...}}` + a `run_id`, and a
new row on the dashboard. **Proves:** capabilities are a callable, typed API an agent invokes by
name with no UI knowledge; each invocation runs the deterministic replay. `confirm` is **not** an
accepted field; `POST .../meridian_signon/invoke` returns 400.

---

## 8. Chatbot (Terminal B, API still up in A)

```bash
python -m chatbot.cli
you> what's the first share balance and status for member 102777?
you> transfer 1.00 from 100234-S0001 to 100234-MMKT-3 for member 100234
```

**Working =** it prints `· invoking <capability>({...})` then a plain-language answer with the
concrete value / business outcome / "routed to a human". `Ctrl-D` to quit. One Claude call to
route + one to phrase the result; the `/invoke` under it has no LLM.

---

## 9. Escalation — human in the loop, fully scripted

```bash
python scripts/demo_meridian_escalation.py
```

**Working =**
```
decision='approved' -> status=success outputs={'confirmation': 'CN...'}
decision='declined' -> status=escalated
... DEMO OK
```

**Proves:** a risky capability invoked without `confirm` runs to its final click, then routes an
intervention request (capability, step, reason, URL, screenshot) to the operator console;
approve → it commits, decline → `status: escalated`, nothing posted.

**To do the approval yourself** instead of the scripted approver: invoke the risky capability
through the API/chatbot, then open **http://localhost:5001** (login `banker` /
`demo-operator-pw`), read the reason + screenshot, click **Approve & Resume** or **Decline &
Resume**.

---

## Backup (the network will not be your friend)

Everything above has already been run; the outputs are committed:

- `evidence/runs.jsonl` — every discovery + replay run (id, capability, status, outputs, timings)
- `evidence/discovery_run_*.jsonl` — real LLM transcripts
- `evidence/replay_*.json` — replay results incl. every `--inject` kind and the business outcomes
- `evidence/demo_meridian_escalation.json` — approve + decline
- `evidence/escalation_*_context.json` + `*.png` — the intervention context + screenshot

Also record a 2-minute screen capture of steps 3–4, the chatbot, and one operator-console
approval before demo day.

---

## Troubleshooting

| symptom | fix |
|---|---|
| transfer → `INSUFFICIENT_FUNDS` / `SOURCE_SHARE_ON_HOLD` | that share got drained/held by earlier runs. `python scripts/meridian_scout.py 100234` and use fresh ids. Resets on the app's next redeploy. |
| hold → `select_option ... did not find some options` | the hold form only lists **un-held** shares. Use `meridian_scout.py`'s `holdable` value, or a member you haven't held. |
| a run hangs on "Waiting for operator to resume" | a stale operator console on :5001. `lsof -ti :5001 \| xargs kill -9` and `rm -f escalation/state/lease.json escalation/state/resume.signal`. |
| `AuthenticationError: API key is invalid` | `ANTHROPIC_API_KEY` in `.env` is stale — replace it, re-run `set -a; source .env; set +a`. |
| operator console 401 | log in with `OPERATOR_USERNAME` / `OPERATOR_PASSWORD` from `.env` (or the one-time creds it printed at startup). |
| macOS port 5000 quirk | not relevant here — the API is on 8000, operator console 5001. |
