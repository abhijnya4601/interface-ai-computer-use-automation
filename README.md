# Computer-use automation: hands for an AI agent

A small end-to-end system where a language model drives a real, messy web app to get a job
done, that successful run is turned into a typed and versioned artifact, and the artifact is
then replayed deterministically with no model in the loop. Around that core there is
runtime-error and business-outcome handling, safety guardrails, redaction, a human-in-the-loop
escalation path, and a link-shareable web console that lets someone watch all of it happen
live.

This branch is where all the current work lives:

> **https://github.com/abhijnya4601/interface-ai-computer-use-automation/tree/abhijnya/live-console**

```bash
git clone https://github.com/abhijnya4601/interface-ai-computer-use-automation.git
cd interface-ai-computer-use-automation
git checkout abhijnya/live-console
```

---

## The short version

There are two phases, and the seam between them is the whole point.

**Discovery** happens once. The model is given a plain-English goal and an entry URL. Each turn
it sees the page as an accessibility tree (roles, names, values, the way a screen reader would),
picks one tool call (click, type, navigate, extract, finish, escalate), and acts. Nothing about
the step sequence is written by hand. When it finishes, the recorded steps are compiled into a
**capability**: a JSON file with typed inputs and outputs, a locator strategy per step, declared
business-outcome branches, a success checkpoint, and a data-shape contract for each value it
reads.

**Replay** happens every time after that. `replay()` walks the capability's steps against a
fresh page with different inputs. No model call, so it is fast, free, and repeatable. It knows
the difference between a real failure, a normal business result ("no such member", "account
locked"), a degraded service it can recover from, and a page that loaded but had no data behind
the field it needed.

The capability is a plain file. A person can read it, edit a fragile locator, tighten a
contract, run `scripts/verify_capability.py` to replay every declared branch, and
`scripts/review_capability.py` to promote it from `draft` to `verified`. Discovery gives you a
draft; a developer polishes it if it needs it.

---

## What you can run

| Thing | Command | Needs |
|---|---|---|
| Live console (the main artifact) | `make app` then `make console` | a browser; a model key only for the Chatbot/Discover tabs |
| A second target with worse markup and fault injection | `make app2` | a browser |
| The assistant from the terminal | `python scripts/assistant_cli.py "..."` | a model key |
| One discovery run | `python scripts/run_discovery.py --goal "..." --target ... --capability-id ...` | a browser + a model key |
| One replay | `python scripts/run_replay.py --capability capabilities/<name>.v1.json --params '{...}'` | a browser |
| The test suite | `make test` (or `pytest -q`) | nothing (no browser, no key) |

Model keys work for **Anthropic, OpenAI, or Google (Gemini)**. Set `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, or `GEMINI_API_KEY`, or paste one into the console UI. A pasted key is used
for that one run and is never written to disk or logged.

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

make app        # terminal 1: mock core-banking app on :5050
make app2       # terminal 2: hostile-DOM app on :5051 (optional)
make console    # terminal 3: prints http://localhost:5055/?key=<token>
```

Open the printed URL. Pick a target and a capability, press **Run**, and watch the agent
console and the real browser update side by side. Replay needs no key. For the Chatbot and
Discover tabs, choose a provider and paste a key.

Everything in Docker instead:

```bash
docker compose up --build bank hostile console
```

---

## The live console

`webconsole/` is a link-gated web page. Whoever has the link with the `?key=` token can drive a
run; a cookie carries the key for 30 days after the first visit. One run at a time.

**Replay tab.** Pick a compiled capability, set its inputs, run it. You see every step, its
locator tier, its timing, and its outcome. Try member `12345` (fine), `88888` (not found),
`99999` (locked), `77777` (page loads, the data field is empty).

**Chatbot tab.** Type a request in plain English. `agent_interface/assistant.py` plans it into
tasks and carries each out **in order**: a task that matches a capability the system already has
runs deterministically with no model call; a task with none triggers a discovery run and the
new capability is saved for next time. A request with several parts ("read the name and the
balance") runs each part and merges the outputs into one answer. One planning call plus one
phrasing call per request, whatever the task count.

**Discover tab.** Give a goal, watch the loop run turn by turn, see the redaction pass on each
observation, and see the compiled capability land in the Replay list. A newly compiled safe
capability is replayed once on the spot as a self-check; if it does not hold up, the console
says so right away rather than letting you find out at first use.

**Escalation.** Before any step that changes data, the run checks an editable policy
(`escalation/rules.yaml`, or the panel in the console). If a rule says to escalate, or the
model asks for a human itself, the run pauses, a banner shows on the page, and an Approve or
Decline panel opens. Approve continues the same browser session; Decline ends the run as a
declined outcome. Two rules ship by default: any risky capability pauses at its confirm step,
and an opening deposit above 500 pauses.

**History tab.** Every run on the instance: kind, capability, status, business outcome,
escalation count, duration. Newest first. Params and outputs are redacted.

**Data handling panel.** A plain-language explanation of the redaction model for reviewers,
tied to what the console actually shows on each run.

---

## Design writeup

### Perception is the accessibility tree, not the DOM

The model never sees raw HTML. `agent/perception.py` turns the page into a pruned tree of
roles, accessible names, and values, and merges iframe content into it. This is what makes a
locator like `role="button" name="Confirm and Open Account"` work whether or not that button is
inside a confirmation iframe, and it means a capability survives CSS and class-name churn. It
only breaks if the visible label text or the semantic role itself changes.

The recorder builds a locator per step in tiers: a unique role-plus-name match is tier 1; more
than one match falls back to a structural position; no match at all falls back to raw text. A
tier below 1 is fragile, and both discovery and replay know that.

### The capability is the contract

`artifact/schema.py` defines it. Key parts:

- `input_schema` / `output_schema`: typed, so replay can be called with new values and its
  result read back structurally.
- `steps`: each with an action, a locator target with fallbacks, a wait policy, and a list of
  `expected_outcomes` that map a condition on the page to a classification (business outcome,
  recoverable, hard failure, or data unavailable).
- `checkpoint`: how replay confirms it landed in the right place at the end.
- `extract_contract` per output: a pattern and a set of placeholder strings, so a value that
  came back empty or malformed is reported as data-unavailable instead of a silent wrong
  answer.
- `lifecycle`: `draft` when freshly compiled (may carry rules a human has not ratified) or
  `verified` once every declared branch has replayed clean.

Per-app knowledge is not baked into code. `app_knowledge/<app>.yaml` holds the curated
branches, contracts, and checkpoints for one target. The discovery agent proposes these from
what it actually saw; a reviewer promotes the good ones into the YAML.

### Replay decides outcomes by checking conditions, not by guessing

`replay/engine.py` runs the steps. After each action it checks the step's declared conditions
against the live page, because even a successful click can land on a locked-account page. The
possible run statuses are `success`, `business_outcome` (with a code), `recoverable_handled`
(a declared transient state, stopped cleanly), `data_unavailable` (the page was there, the
datum was not), and `hard_failure`. Retries only happen on steps explicitly marked for it, not
blindly.

### Partial observability

A recurring question is what happens with page access but no data access. The answer is the
`data_unavailable` status and the `extract_contract`. If replay reaches a member page and the
balance cell is blank or holds a placeholder, the contract rejects it and the run reports
data-unavailable with the observed value and the reason, rather than returning an empty string
as if it were the balance. The hostile-DOM app's `?inject=blank` switch simulates exactly this.

### The assistant is a reusable core, not a console feature

`agent_interface/assistant.py` owns the "plan a request into tasks, reuse a capability where
one exists, discover where none does, run in order, merge outputs" logic. It is provider- and
UI-agnostic: the caller injects a "run a capability" function, a "run discovery" function, and
an event sink. The console's Chatbot tab is one caller. `scripts/assistant_cli.py` is a
headless one. The cost model is the same either way: one planning call and one phrasing call
per request, zero model cost for a task that already has a capability, and a one-time discovery
cost the first time a task is genuinely new.

Discovery is made more reliable in three ways so that hand-fixing is the exception:

1. After a safe capability compiles, it is replayed once with the discovery inputs. A fragile
   extract or a bad checkpoint fails here and is flagged at creation.
2. When the agent extracts a value with a weak locator, the tool result tells it so, so it can
   navigate to a labelled view and re-anchor in the same run.
3. The discovery prompt asks the model to read a value where it sits in a labelled row or
   field, not from a bare table cell or loose text.

### Safety

- `guardrails/allowlist.yaml` lists the domains and action types allowed. It is checked before
  every action in both discovery and replay, and a violation halts the run. Discovery has its
  own stricter list, because every discovery turn sends page content to a third-party model.
- `guardrails/pii.py` does sink-aware redaction. The same value is treated differently
  depending on where it is going: a declared output in a saved artifact, a line in an evidence
  log, or page content on its way to the model. The model-prompt sink is the strictest and
  also masks ZIP and long-digit shapes. Ambiguous bare numbers, like a member id, are flagged
  and left intact rather than masked, because masking them would break navigation, and the
  per-run report records what was flagged. Microsoft Presidio is used if installed, with a
  deterministic regex pass as the fallback so redaction never silently does nothing.
- Bring-your-own keys are held in memory for one run and never persisted.

### The two target apps

- `app/` is a mock core-banking app on :5050. Legacy layout, ugly class names, a nested-table
  search result, a confirmation step inside an iframe, but honest semantic HTML underneath, so
  the accessibility tree is clean.
- `app2/` is a hostile-DOM app on :5051. Clickable divs instead of buttons, no `<label for>`,
  values buried in nested spans, class-name soup. ARIA roles are kept, so role-plus-name still
  resolves, but this exercises the messy-markup path and the label-proximity fallbacks. It also
  has a `?inject=` switch: `maintenance` returns 503 on every request and maps to
  `recoverable_handled`, `slow` adds latency, `blank` renders the value field empty and maps to
  `data_unavailable`. The console surfaces this as a Fault injection dropdown.

---

## Layout

```
agent/            perception, the discovery loop, the recorder, the tool surface, the
                  provider-agnostic LLM layer (agent/llm.py)
agent_interface/  the assistant, the capability catalog, deterministic invoke, the run registry
artifact/         the Capability schema and the base-plus-per-tenant patch mechanism
replay/           the deterministic engine, idempotency ledger, verification, metrics
guardrails/       allowlist enforcement, sink-aware redaction, encryption-at-rest
escalation/       the automation-to-human lease, the policy engine, the operator page
webconsole/       the link-gated live console (Flask + server-sent events)
app/  app2/       the two target apps
app_knowledge/    curated per-app branches, contracts, checkpoints (YAML, not code)
capabilities/     the compiled capability artifacts
scripts/          run_discovery, run_replay, assistant_cli, verify_capability,
                  review_capability, and the demo and smoke scripts
```

---

## Testing

```bash
make test          # 336 tests, no browser, no key, about 3 seconds
make lint          # ruff
```

The suite covers the schema, the guardrails and redaction, perception parsing, the recorder's
locator tiers, the compiler, the replay engine's pure logic, the escalation lease and policy,
the assistant orchestration, the console's HTTP surface, and both target apps. Anything that
needs a real browser or a real key is a script under `scripts/`, run on purpose, not in the
suite.

There is also a manual walkthrough for exercising every part of the console by hand: pick a
target and member id, run the outcome variants, toggle fault injection, ask the Chatbot a
single and a compound request, run a discovery, trigger and resolve an escalation, edit a rule,
and check the History tab.

---

## Deploy

`webconsole/serve.sh` is the single-container entrypoint: it starts both target apps internally
and the console on `$PORT`. See [`DEPLOY.md`](DEPLOY.md) for the free options (a Docker web
service, or a tunnel from a machine you keep on) and the paid ones. Set `CONSOLE_ACCESS_KEY` to
a value you treat as a secret; that is the link token.

---

## Honest limits

- OpenAI and Gemini are wired through `agent/llm.py` and their keys are accepted, but the
  Anthropic path is the one exercised end to end here. A first real run on another provider may
  need a model-id or parameter tweak.
- On a 512 MB free host, a heavy discovery run can run out of memory; replay is fine.
- On a host with no persistent disk, capabilities discovered through the console reset on
  redeploy. The curated ones reseed.
- Discovery is a best-effort learning pass. The self-check, the weak-locator feedback, and the
  prompt guidance reduce how often it produces something that needs a fix, and the review
  scripts make fixing one a supported step, but it is not guaranteed to be perfect on the first
  try for a hard target.
