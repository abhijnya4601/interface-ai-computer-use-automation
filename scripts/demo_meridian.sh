#!/usr/bin/env bash
# End-to-end MERIDIAN CORE demo — the exact sequence for demo day.
#
#   1. deterministic replay of a recorded capability against the live target  (happy path)
#   2. the same capability hitting an exceptional state, detected + reported cleanly
#   3. a business outcome that is NOT a crash
#   4. pointers for the API + chatbot + dashboard (interactive) and the escalation demo
#
# Needs:  ANTHROPIC_API_KEY (only for the chatbot), MERIDIAN_OPERATOR, MERIDIAN_PASSWORD,
#         optionally MERIDIAN_SUPERVISOR_OPERATOR / MERIDIAN_SUPERVISOR_PASSWORD.
# Everything below is replay only — no LLM, no API key required for steps 1–3.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && { set -a; . ./.env; set +a; }

: "${MERIDIAN_OPERATOR:?set MERIDIAN_OPERATOR (e.g. teller1)}"
: "${MERIDIAN_PASSWORD:?set MERIDIAN_PASSWORD (e.g. password)}"

PY=${PYTHON:-python3}
CAP=capabilities/meridian_check_member_balance.v1.json
run() { echo; echo "\$ $*"; "$@"; }

echo "=================================================================="
echo " 1. HAPPY PATH — deterministic replay, member never used to record"
echo "=================================================================="
run $PY scripts/run_meridian.py --capability "$CAP" \
    --params '{"member_id":"100987"}' --label demo_happy

echo
echo "=================================================================="
echo " 2. EXCEPTIONAL STATE — injected 503 maintenance on the same flow"
echo "    (expect: status=recoverable_handled, not a crash)"
echo "=================================================================="
run $PY scripts/run_meridian.py --capability "$CAP" \
    --params '{"member_id":"100987"}' --inject maintenance --label demo_inject_503 || true

echo
echo "=================================================================="
echo " 3. BUSINESS OUTCOME — a member that does not exist"
echo "    (expect: status=business_outcome, code=MEMBER_NOT_FOUND)"
echo "=================================================================="
run $PY scripts/run_meridian.py --capability "$CAP" \
    --params '{"member_id":"404404"}' --label demo_not_found || true

echo
echo "=================================================================="
echo " 4. DEMO-SAFE INPUTS  (the app is stateful; it only resets on redeploy)"
echo "=================================================================="
run $PY scripts/meridian_scout.py 100234 103001 || true
echo "  ^ use those share ids for the transfer / hold examples below."

echo
echo "=================================================================="
echo " 5. INTERACTIVE — run these in two terminals for the live demo"
echo "=================================================================="
cat <<'EOF'

  # terminal A — capability API + dashboard + (lazy) operator console
  python -m api
      -> http://localhost:8000        dashboard (catalog + run history + evidence)
      -> http://localhost:5001        operator console (starts on the first risky invoke)

  # terminal B — chatbot over the API (needs ANTHROPIC_API_KEY)
  python -m chatbot.cli
      you> what is the first share balance and status for member 101555?
      you> transfer 1.00 from 100234-MMKT-10 to 100234-S0001-11 for member 100234
           -> the risky transfer pauses; approve it at http://localhost:5001, then it posts

  # or drive the API directly
  curl -s localhost:8000/api/capabilities | python3 -m json.tool
  curl -s -X POST localhost:8000/api/capabilities/meridian_check_member_balance/invoke \
       -H 'content-type: application/json' -d '{"args":{"member_id":"100987"}}' | python3 -m json.tool

  # fully-scripted escalation (real browser, real operator process, real HTTP, no clicking):
  python scripts/demo_meridian_escalation.py

EOF
echo "Evidence for every step above is written to evidence/  (replay_demo_*.json, runs.jsonl)."
