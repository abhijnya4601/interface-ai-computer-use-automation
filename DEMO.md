# MERIDIAN CORE — setup & live demo guide

Everything needed to set up the adaptation and run it end to end against the hosted target
**`web-sample.interface-hiring.com`**, with the exact commands, what each step proves, and what
"working" looks like. Design write-up: [`ADAPTATION.md`](ADAPTATION.md). The original take-home
(local mock app) is [`README.md`](README.md).

Every command block below has a **Windows (PowerShell)** toggle directly under it — expand that
instead of translating by hand.

---

## Contents

1. [What you need](#1-what-you-need)
2. [Setup](#2-setup)
3. [Every terminal, first](#3-every-terminal-first)
4. [Pre-flight checks](#4-pre-flight-checks)
5. [Running offline / without the target](#5-running-offline--without-the-target)
6. [The demo, step by step](#6-the-demo-step-by-step)
7. [Watching it visually](#7-watching-it-visually)
8. [The four surfaces — who uses what](#8-the-four-surfaces--who-uses-what)
9. [Backup for demo day](#9-backup-for-demo-day)
10. [Troubleshooting](#10-troubleshooting)
11. [Stopping everything](#11-stopping-everything)

---

## 1. What you need

- **Python 3.11+** (built and tested on 3.14).
- **git**.
- An **Anthropic API key** (`sk-ant-…`) — only for the discovery run and the chatbot. Every
  replay / deterministic step needs no key.
- Playwright downloads its own Chromium (no admin rights needed).
- macOS, Linux, or Windows 10/11.

Nothing needs to be installed on the target — it's hosted and already running; the demo operator
credentials are public (`teller1` / `password`, `super1` / `password`).

---

## 2. Setup

### 2.1 Clone and switch to the adaptation branch

```bash
git clone https://github.com/abhijnya4601/interface-ai-computer-use-automation.git
cd interface-ai-computer-use-automation
git checkout adaptation-meridian-core
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
git clone https://github.com/abhijnya4601/interface-ai-computer-use-automation.git
cd interface-ai-computer-use-automation
git checkout adaptation-meridian-core
```

</details>

### 2.2 Virtual environment + dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

> If activation is blocked with *"running scripts is disabled on this system"*, allow it for
> this session only:
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

</details>

### 2.3 The `.env` file (gitignored, never committed)

```bash
cat > .env <<'ENV'
ANTHROPIC_API_KEY=sk-ant-...

# MERIDIAN demo operators — public, no real data
MERIDIAN_OPERATOR=teller1
MERIDIAN_PASSWORD=password
MERIDIAN_BRANCH=MAIN-001
MERIDIAN_SUPERVISOR_OPERATOR=super1
MERIDIAN_SUPERVISOR_PASSWORD=password

# operator console (escalation approvals) — a stable credential so you don't hunt for a generated one
OPERATOR_USERNAME=banker
OPERATOR_PASSWORD=demo-operator-pw
ENV
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
@"
ANTHROPIC_API_KEY=sk-ant-...

MERIDIAN_OPERATOR=teller1
MERIDIAN_PASSWORD=password
MERIDIAN_BRANCH=MAIN-001
MERIDIAN_SUPERVISOR_OPERATOR=super1
MERIDIAN_SUPERVISOR_PASSWORD=password

OPERATOR_USERNAME=banker
OPERATOR_PASSWORD=demo-operator-pw
"@ | Out-File -Encoding ascii .env
```

</details>

---

## 3. Every terminal, first

Load the venv and the environment variables into the current shell:

```bash
cd interface-ai-computer-use-automation
source .venv/bin/activate
set -a; source .env; set +a
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
cd interface-ai-computer-use-automation
.venv\Scripts\Activate.ps1
Get-Content .env | ForEach-Object {
    $name, $value = $_.Split('=', 2)
    if ($name -and -not $name.StartsWith('#')) { Set-Item "Env:$name" $value }
}
```

</details>

> On Windows use `python` everywhere the bash blocks say `python3`.

---

## 4. Pre-flight checks

Run these before you present.

```bash
python3 -m pytest tests/ -q
curl -sS -o /dev/null -w "%{http_code}\n" https://web-sample.interface-hiring.com/signon
python3 scripts/meridian_scout.py 100234 103001
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
python -m pytest tests/ -q
(Invoke-WebRequest -UseBasicParsing https://web-sample.interface-hiring.com/signon).StatusCode
python scripts\meridian_scout.py 100234 103001
```

</details>

**Working =**
- `194 passed` (offline, ~6s, no network).
- `200` from the target.
- `meridian_scout.py` prints a table of shares usable **right now**. This matters: the hosted
  app is **stateful in memory** and only resets on redeploy, so repeated risky runs move
  balances and place holds. Use the ids it prints for steps 6.4–6.5 and the chatbot.

```
member     from_share (OPEN, >$0)   to_share (OPEN)        holdable share
100234     100234-MMKT-3            100234-S0001-5         100234-S0001
103001     ...
```

---

## 5. Running offline / without the target

`python3 -m pytest tests/` (Windows: `python -m pytest tests/`) is fully offline — 194 tests
covering the legacy-form locator adapter, the session module, the runtime/outcome taxonomy, the
recovery loop, the run registry, and the API (Flask test client with the browser path stubbed).

There is **no mock of the target** — MERIDIAN CORE must be reachable for any replay or discovery
run. That's the point of the exercise.

---

## 6. The demo, step by step

### 6.1 A real LLM discovery run — the heart of it

```bash
python3 scripts/run_discovery.py \
  --goal "Search for member 100987 by member number and click Select. Then extract the first SHARES/BALANCES row: Share ID as share_id, Type as share_type, Balance as balance, Status as status. Finish with all four in outputs." \
  --target https://web-sample.interface-hiring.com/members \
  --capability-id demo_lookup --headless
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
python scripts\run_discovery.py `
  --goal "Search for member 100987 by member number and click Select. Then extract the first SHARES/BALANCES row: Share ID as share_id, Type as share_type, Balance as balance, Status as status. Finish with all four in outputs." `
  --target https://web-sample.interface-hiring.com/members `
  --capability-id demo_lookup --headless
```

</details>

**Working =** `status=success`, then `capability saved to capabilities/demo_lookup.v1.json` and
`transcript saved to evidence/discovery_run_<id>.jsonl`.

**Proves:** an LLM drives the real legacy UI (no clean DOM, no test IDs, hidden per-transaction
token, table layout) turn by turn — observe → decide one action → act — and the successful run
is compiled into a typed, versioned capability artifact.

Open the transcript — every line is a real model tool call:

```bash
T=$(ls -t evidence/discovery_run_*.jsonl | head -1)
python3 -c "import json;[print(' ',e['name'],e['input']) for l in open('$T') for e in [json.loads(l)] if e['type']=='tool_call']"
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
$T = Get-ChildItem evidence\discovery_run_*.jsonl | Sort-Object LastWriteTime -Desc | Select -First 1
python -c "import json,sys;[print(' ',e['name'],e['input']) for l in open(sys.argv[1]) for e in [json.loads(l)] if e['type']=='tool_call']" $T.FullName
```

</details>

> **For a live audience, don't gamble on a fresh discovery run** — the model is
> non-deterministic and occasionally fumbles a step. Show a **committed** transcript instead:
> `evidence/discovery_run_c612111ab2.jsonl` (that's what `capabilities/meridian_check_member_balance.v1.json` was built from). Every one of the 7 §2.1 capabilities was
> produced by a real discovery run — see `created_from_run_id` in each file.

### 6.2 Deterministic replay — same artifact, no LLM, a different member

```bash
python3 scripts/run_meridian.py \
  --capability capabilities/meridian_check_member_balance.v1.json \
  --params '{"member_id":"100234"}'
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
python scripts\run_meridian.py `
  --capability capabilities\meridian_check_member_balance.v1.json `
  --params '{"member_id":"100234"}'
```

</details>

**Working =** `status: success` and `outputs` with **member 100234's own** first-share row
(the capability was recorded off member 101555). The tier log prints `labeled_field` for the
search box and `table_position` for the four cells — position-anchored, so the extracted values
are never part of the locator.

**Proves:** genuine parameterisation and deterministic replay — no model in the decision loop.
`scripts/run_meridian.py` signs on (credentials from the environment) then replays the target
capability on that same authenticated session.

### 6.3 Exceptional states — detected and reported, never crashes

```bash
for k in validation notfound permission timeout maintenance server; do
  printf "%-12s -> " "$k"
  python3 scripts/run_meridian.py --capability capabilities/meridian_check_member_balance.v1.json \
    --params '{"member_id":"100234"}' --inject $k --label demo_$k 2>&1 \
    | grep -E '^status:|^business_outcome_code:|^recovery:' | tr '\n' ' '; echo
done
python3 scripts/run_meridian.py --capability capabilities/meridian_check_member_balance.v1.json \
  --params '{"member_id":"999999"}' --label demo_notseeded 2>&1 | grep -E '^status:|^business_outcome_code:'
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
foreach ($k in 'validation','notfound','permission','timeout','maintenance','server') {
  Write-Host -NoNewline ("{0,-12} -> " -f $k)
  python scripts\run_meridian.py --capability capabilities\meridian_check_member_balance.v1.json `
    --params '{"member_id":"100234"}' --inject $k --label ("demo_" + $k) 2>&1 `
    | Select-String '^status:|^business_outcome_code:|^recovery:' | ForEach-Object { $_.Line } | Join-String -Separator '  '
}
python scripts\run_meridian.py --capability capabilities\meridian_check_member_balance.v1.json `
  --params '{"member_id":"999999"}' --label demo_notseeded 2>&1 | Select-String '^status:|^business_outcome_code:'
```

</details>

**Working =**

| trigger | expect |
|---|---|
| `validation` (HTTP 400) | `business_outcome` · `VALIDATION_REJECTED` |
| `notfound` (404) | `business_outcome` · `RECORD_NOT_FOUND` |
| `permission` (403) | `business_outcome` · `PERMISSION_DENIED` |
| `timeout` (440) | `recoverable_handled` · `recovery: [... reauth_and_retry ... gave_up]` |
| `maintenance` (503) | `recoverable_handled` · `recovery: [... retry ... gave_up]` |
| `server` (500) | `hard_failure` · `SERVER_ERROR` |
| member 999999 (not seeded) | `business_outcome` · `MEMBER_NOT_FOUND` |

**Proves:** the three-class result contract — a **business outcome** ("no such member") is a real
answer, not a crash; a **recoverable** condition (440/503) is retried in a bounded, declared way
(503 → retry ×3, 440 → re-authenticate then retry) and only then reported; a **hard failure**
(500) stops with a debuggable error. `--inject <kind>` appends `?inject=<kind>` to the entry
navigation at replay time; the saved capability is unchanged. A *forced* inject never clears, so
recovery reports `gave_up` — a transient error rate would `recover` and the run would continue.

### 6.4 A risky capability — the confirmation gate

Use `from_share` / `to_share` from `scripts/meridian_scout.py 100234`.

```bash
FROM=100234-MMKT-3 ; TO=100234-S0001-5

# without --confirm: refused before touching the page
python3 scripts/run_meridian.py --capability capabilities/meridian_funds_transfer.v1.json \
  --params "{\"member_id\":\"100234\",\"from_share\":\"$FROM\",\"to_share\":\"$TO\",\"amount\":\"1.00\",\"memo\":\"demo\"}"

# with --confirm: actually posts
python3 scripts/run_meridian.py --capability capabilities/meridian_funds_transfer.v1.json \
  --params "{\"member_id\":\"100234\",\"from_share\":\"$FROM\",\"to_share\":\"$TO\",\"amount\":\"1.00\",\"memo\":\"demo\"}" --confirm
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
$FROM = '100234-MMKT-3'; $TO = '100234-S0001-5'
$p = '{"member_id":"100234","from_share":"' + $FROM + '","to_share":"' + $TO + '","amount":"1.00","memo":"demo"}'

python scripts\run_meridian.py --capability capabilities\meridian_funds_transfer.v1.json --params $p
python scripts\run_meridian.py --capability capabilities\meridian_funds_transfer.v1.json --params $p --confirm
```

</details>

**Working =** no `--confirm` → `status: hard_failure` (`confirm=True required`, refused before
the browser launches); with `--confirm` → `status: success`, `outputs: {'confirmation': 'CN…'}`,
a real ledger post.

**Proves:** irreversible actions are gated; `--confirm` (a decision only trusted code / a human
makes) is what commits. Same for `meridian_open_share` and `meridian_place_hold`.

### 6.5 A supervisor-gated capability

```bash
HOLD=100234-S0001   # from meridian_scout.py's "holdable share" column

# as a teller -> refused as a business outcome
python3 scripts/run_meridian.py --capability capabilities/meridian_place_hold.v1.json \
  --params "{\"member_id\":\"100234\",\"share\":\"$HOLD\",\"reason\":\"LEGAL\",\"notes\":\"demo\"}" --confirm --role teller

# as a supervisor -> posts
python3 scripts/run_meridian.py --capability capabilities/meridian_place_hold.v1.json \
  --params "{\"member_id\":\"100234\",\"share\":\"$HOLD\",\"reason\":\"LEGAL\",\"notes\":\"demo\"}" --confirm --role supervisor
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
$HOLD = '100234-S0001'
$p = '{"member_id":"100234","share":"' + $HOLD + '","reason":"LEGAL","notes":"demo"}'

python scripts\run_meridian.py --capability capabilities\meridian_place_hold.v1.json --params $p --confirm --role teller
python scripts\run_meridian.py --capability capabilities\meridian_place_hold.v1.json --params $p --confirm --role supervisor
```

</details>

**Working =** teller → `business_outcome` · `PERMISSION_DENIED`; supervisor → `status: success`,
a confirmation number, and the hold is recorded with the reason code you passed.

**Proves:** `Capability.requires_role` — the session signs on with the right credential
(`MERIDIAN_SUPERVISOR_*`); a teller gets a clean business outcome, a supervisor completes it.

### 6.6 The API + dashboard

**Terminal A** — the capability API and dashboard (leave it running):

```bash
python3 -m api
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
python -m api
```

</details>

Then in your browser open **http://localhost:8000**:

- **Capabilities** table — all 7 §2.1 capabilities, with `meridian_signon` shown as
  `(precondition)` (it's composed automatically before the others, not invoked directly).
- **Runs** table — the full history, **discovery and replay**, newest first, with status badges.
  Click a run for its inputs, structured outputs, and evidence (screenshots inline).
- The operator console (http://localhost:5001) starts automatically on the first risky invoke.

**Terminal B** — invoke a capability over HTTP:

```bash
curl -sS -X POST http://localhost:8000/api/capabilities/meridian_check_member_balance/invoke \
  -H 'content-type: application/json' -d '{"args":{"member_id":"101555"}}' | python3 -m json.tool
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
$body = '{"args":{"member_id":"101555"}}'
Invoke-RestMethod -Method Post -ContentType application/json -Body $body `
  -Uri http://localhost:8000/api/capabilities/meridian_check_member_balance/invoke | ConvertTo-Json -Depth 8
```

</details>

**Working =** JSON with `"result": {"status": "success", "outputs": {...}}` and a `run_id`; a new
row appears on the dashboard.

**Proves:** capabilities are a callable, typed API — an agent invokes one by name with typed
args and gets a structured result, without knowing anything about the UI. `confirm` is **not** an
accepted field. `POST /api/capabilities/meridian_signon/invoke` returns **400**.

Other endpoints: `GET /api/capabilities`, `GET /api/runs`, `GET /api/runs/<id>`,
`GET /api/runs/<id>/evidence/<name>`, `GET /api/health`.

### 6.7 The chatbot

**Terminal B** (API still up in Terminal A):

```bash
python3 -m chatbot.cli
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
python -m chatbot.cli
```

</details>

```
you> what's the first share balance and status for member 102777?
you> transfer 1.00 from 100234-MMKT-3 to 100234-S0001-5 for member 100234
```

**Working =** it prints `· invoking <capability>({...})` then a plain-language answer with the
concrete value, the business outcome, or "routed to a human for approval". `Ctrl-D` to quit.

**Proves:** a conversational front door (standing in for the AI agent — deliberately a thin CLI,
not a second product) turns a request into the right capability invocation and reports the
structured result in plain language. One model call to route + one to phrase; the `/invoke`
underneath has no model in it.

### 6.8 Escalation — human in the loop

Fully scripted (real browser, a real separate operator-console process, real HTTP, no clicking):

```bash
python3 scripts/demo_meridian_escalation.py
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
python scripts\demo_meridian_escalation.py
```

</details>

**Working =**
```
decision='approved' -> status=success outputs={'confirmation': 'CN...'}
decision='declined' -> status=escalated
... DEMO OK
```

**To do the approval yourself:** invoke the risky capability through the API or chatbot, then
open **http://localhost:5001** (log in `banker` / `demo-operator-pw`), read the reason and the
screenshot of exactly where the automation paused, and click **Approve & Resume** or
**Decline & Resume**. See §8 for how the console works.

**Proves:** a risky capability invoked without `confirm` runs to its final click, then **pauses
the live session**, routes an intervention request (which capability, current step, reason,
current URL, a screenshot) to a human operator, and only commits on approval; a decline returns
`status: escalated` with nothing posted. Control transfers on the **same** browser session, not
a fresh one.

---

## 7. Watching it visually

Most scripts run headless. To watch the browser drive the site, add `--headed`:

```bash
python3 scripts/run_meridian.py --capability capabilities/meridian_check_member_balance.v1.json \
  --params '{"member_id":"100234"}' --headed
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
python scripts\run_meridian.py --capability capabilities\meridian_check_member_balance.v1.json `
  --params '{"member_id":"100234"}' --headed
```

</details>

A Chromium window opens, signs on, searches, clicks through, and stays up 5 seconds on the final
page. `scripts/run_discovery.py` is headed by default (pass `--headless` to hide it).

To open the browser surfaces:

```bash
open https://web-sample.interface-hiring.com/signon     # the raw target
open http://localhost:8000                              # dashboard (API must be running)
open http://localhost:5001                              # operator console (starts on first risky invoke)
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
Start-Process https://web-sample.interface-hiring.com/signon
Start-Process http://localhost:8000
Start-Process http://localhost:5001
```

</details>

---

## 8. The four surfaces — who uses what

This system is the **hands**, not the face: the agent-facing product decides *what* to do; this
system is *how* it does it, reliably and safely, inside legacy bank software with no API.

| Surface | Who | In this repo | In a real deployment |
|---|---|---|---|
| Conversational agent | a bank customer / staff member | `chatbot/cli.py` — a **CLI stand-in** | interface.ai's actual agent product (chat widget, Slack, phone) — not this repo |
| Capability API | the agent (code, not a person) | `POST /api/capabilities/<id>/invoke` | the same API |
| Dashboard | interface.ai engineers / the bank's automation team | `localhost:8000`, read-only | a hardened version of this — internal ops tooling |
| Operator console | a bank supervisor pulled in on a risky or stuck action | `localhost:5001`, bare 3 buttons — a **deliberate mock** | a real co-browsing console (out of scope; the handoff mechanism is the real part) |

Flow: a person talks to the agent → the agent calls `POST /invoke` → this system replays the
capability against MERIDIAN CORE → if the action is risky/irreversible it pauses and asks a human
operator → returns a structured result to the agent → the agent answers the person. The bank's
staff and customers never touch this system directly.

**How the operator console works.** When the automation must not proceed alone (an irreversible
transfer, a supervisor-only hold, or it's stuck), it writes a small file-backed **lease** flipping
control from `automation` to `human`, captures a screenshot + the reason + the current URL, and
**blocks**. A human opens the console (HTTP Basic Auth), sees exactly why and where it stopped,
and clicks Approve / Decline. That writes a resume signal; the automation picks it up within a
second, flips the lease back to `automation`, re-observes the page, and continues **on the same
session**. The 3-button page is minimal by design; the pause / lease / resume-on-same-session
mechanism underneath is real.

---

## 9. Backup for demo day

Everything above has already been run and its output is committed under `evidence/`:

- `evidence/runs.jsonl` — every discovery and replay run (id, capability, status, outputs, timings).
- `evidence/discovery_run_*.jsonl` — real model transcripts, one tool call per line.
- `evidence/replay_*.json` — replay results, including every `--inject` kind and each business outcome.
- `evidence/demo_meridian_escalation.json` — an approve and a decline.
- `evidence/escalation_*_context.json` + `*.png` — the intervention context and the screenshot the operator sees.

Also record a short screen capture of §6.2, §6.3, the chatbot, and one operator-console approval
before the day — the network is not reliable under pressure.

---

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| transfer → `INSUFFICIENT_FUNDS` / `SOURCE_SHARE_ON_HOLD` | that share was drained / held by earlier runs. `python3 scripts/meridian_scout.py 100234` and use fresh ids. Resets on the app's next redeploy. |
| hold → `select_option ... did not find some options` | the hold form only lists **un-held** shares. Use `meridian_scout.py`'s `holdable` value, or a member you haven't held. |
| a run hangs on *"Waiting for operator to resume"* | a stale operator console on `:5001`. Kill it (see §11) and `rm -f escalation/state/lease.json escalation/state/resume.signal` (PowerShell: `Remove-Item escalation\state\*.json,escalation\state\*.signal -ErrorAction SilentlyContinue`). |
| `AuthenticationError: API key is invalid` | `ANTHROPIC_API_KEY` in `.env` is stale — replace it and re-run the §3 preamble. |
| operator console returns `401` | log in with `OPERATOR_USERNAME` / `OPERATOR_PASSWORD` from `.env` (or the one-time credential it printed at startup). |
| a discovery run dead-ends / `escalation_timeout` | the model got stuck (it's non-deterministic). Re-run it, or use a committed capability. The dead-end detector + bounded escalation wait is the safety net working. |
| PowerShell: `curl` behaves oddly | on Windows, `curl` is an alias for `Invoke-WebRequest`. Use `curl.exe` for the bash-style flags, or the `Invoke-RestMethod` blocks above. |

---

## 11. Stopping everything

```bash
pkill -f 'python3 -m api'; pkill -f 'python -m api'; pkill -f operator_page.py
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'operator_page\.py|-m api' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

</details>

The venv deactivates with `deactivate`. The target and its data are untouched by stopping
anything locally.
