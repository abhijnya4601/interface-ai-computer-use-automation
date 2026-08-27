"""
Run a REAL LLM-driven discovery run for every MERIDIAN CORE §2.1 function, then generalize each
(surface/meridian_flows.py) into a parameterised capability. This is the "discovery for every
function" the brief asks for — replacing the scripted recorders.

Each run:
  - pre-authenticates the session (except sign-on itself, which IS the auth),
  - drives the real Anthropic API against the live target,
  - on success, compiles + generalizes (URL -> {member_id} template, recorded literals -> typed
    params, risk level, requires_role, checkpoint) and overwrites capabilities/meridian_<x>.json.

Existing capabilities are backed up to /tmp first; a run that dead-ends or times out leaves the
current capability in place and is reported as a miss.

    export ANTHROPIC_API_KEY=...  MERIDIAN_OPERATOR=teller1 MERIDIAN_PASSWORD=password \
           MERIDIAN_SUPERVISOR_OPERATOR=super1 MERIDIAN_SUPERVISOR_PASSWORD=password
    python scripts/discover_all_meridian.py [flow ...]      # default: all
"""
from __future__ import annotations

import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).parent.parent
CAPS = REPO / "capabilities"
BACKUP = Path("/tmp/meridian_caps_backup")
BASE = "https://web-sample.interface-hiring.com"

# flow -> (target, extra run_discovery args, goal)
RUNS: dict[str, tuple] = {
    "signon": (
        f"{BASE}/signon",
        ["--no-session", "--max-steps", "8"],
        "Sign on to MERIDIAN CORE. Type operator ID 'teller1' into the Operator ID field, type "
        "'password' into the Password field, set the Branch field to 'MAIN-001 - Main Office', "
        "and click Sign On. Finish once the main menu is shown.",
    ),
    "check_member_balance": (
        f"{BASE}/members",
        ["--max-steps", "12"],
        "Search for member 101555 by member number and click Select to open the record. Then use "
        "the extract tool four times on the first data row of the SHARES / BALANCES table: Share "
        "ID as 'share_id', Type as 'share_type', Balance as 'balance', Status as 'status'. Then "
        "finish with all four in outputs.",
    ),
    "inquiry_by_name": (
        f"{BASE}/members",
        ["--max-steps", "10"],
        "On the Member Inquiry screen: use the type tool on the 'Search by' dropdown with text "
        "'Last Name'. Then use the type tool on the search value field with text 'Lovelace'. "
        "Then click Search. From the first results row, extract the Member No. cell as "
        "'member_no' and the Name cell as 'member_name'. Do not click on individual dropdown "
        "options. Then finish with both values in outputs.",
    ),
    "update_info": (
        f"{BASE}/members/100234/update",
        ["--max-steps", "10"],
        "On the Update Member Information form, replace the E-mail with 'ada.lovelace@example.com', the Phone "
        "with '555-0142', the Mailing Address with '12 Analytical Engine Rd', then click Save Changes. "
        "Finish once the information-updated confirmation is shown.",
    ),
    "funds_transfer": (
        f"{BASE}/members/100234/transfer",
        ["--max-steps", "16", "--auto-approve-escalation"],
        "On the Funds Transfer form set From Share to the option starting '100234-MMKT-10', To "
        "Share to the option starting '100234-S0001-11', Amount to 1.00, Memo to 'recorded "
        "transfer', and click Continue to reach the confirmation screen. Posting is irreversible, "
        "so call escalate for human approval first; after approval click 'Post Transfer' and "
        "finish.",
    ),
    "open_share": (
        f"{BASE}/members/100234/open-share",
        ["--max-steps", "14", "--auto-approve-escalation"],
        "On the Open New Share form set Share Type to the option starting 'MMKT', Initial Deposit "
        "to 5.00, and click Continue. Opening the share is irreversible, so call escalate for "
        "human approval first; after approval click 'Open Share' and finish.",
    ),
    "place_hold": (
        f"{BASE}/members/100234/hold",
        ["--max-steps", "14", "--auto-approve-escalation", "--session-role", "supervisor"],
        "On the Place Account Hold form set Share to the option starting '100234-MMKT-13', Reason "
        "Code to FRAUD, Notes to 'recorded demo hold', and click Continue. Placing a hold is "
        "irreversible, so call escalate for human approval first; after approval click 'Apply "
        "Hold' and finish.",
    ),
}
CAP_ID = {k: f"meridian_{k if k != 'inquiry_by_name' else 'member_inquiry_by_name'}"
          for k in RUNS}
CAP_ID["check_member_balance"] = "meridian_check_member_balance"


def _kill_stray_console() -> None:
    for pat in ("operator_page.py", "chrome-headless-shell.*playwright-profile"):
        subprocess.run(["pkill", "-9", "-f", pat], capture_output=True)
    subprocess.run("lsof -ti :5001 | xargs kill -9", shell=True, capture_output=True)
    for p in ((REPO / "escalation" / "state" / "lease.json"),
              (REPO / "escalation" / "state" / "resume.signal")):
        p.unlink(missing_ok=True)


def run_one(flow: str) -> bool:
    target, extra, goal = RUNS[flow]
    cap_id = CAP_ID[flow]
    cap_file = CAPS / f"{cap_id}.v1.json"
    before = cap_file.read_text() if cap_file.exists() else None
    risky = "--auto-approve-escalation" in extra
    if risky:
        _kill_stray_console()

    cmd = [sys.executable, str(REPO / "scripts" / "run_discovery.py"),
           "--goal", goal, "--target", target, "--capability-id", cap_id,
           "--generalize", flow, "--headless", *extra]
    print(f"\n{'='*70}\n{flow}  ->  {cap_id}\n{'='*70}")
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                              timeout=360, start_new_session=True)
        print("\n".join(proc.stdout.splitlines()[-7:]))
        if proc.returncode != 0:
            print(f"  stderr: {proc.stderr[-400:]}")
    except subprocess.TimeoutExpired:
        print("  TIMEOUT after 360s — killing and moving on")
    _kill_stray_console()

    now = cap_file.read_text() if cap_file.exists() else None
    changed = now is not None and now != before
    status = "OK (capability rewritten by discovery)" if changed else "MISS (kept existing)"
    print(f"  [{time.time()-t0:.0f}s] {status}")
    return changed


def main() -> int:
    flows = sys.argv[1:] or list(RUNS)
    BACKUP.mkdir(exist_ok=True)
    for f in CAPS.glob("meridian_*.json"):
        shutil.copy(f, BACKUP / f.name)
    print(f"backed up {len(list(CAPS.glob('meridian_*.json')))} capabilities to {BACKUP}")

    results = {f: run_one(f) for f in flows if f in RUNS}
    print(f"\n{'='*70}\nSUMMARY")
    for f, ok in results.items():
        print(f"  {f:22} {'discovered + generalized' if ok else 'MISS — existing kept'}")
    misses = [f for f, ok in results.items() if not ok]
    if misses:
        print(f"\n{len(misses)} miss(es): {misses}. The scripted recorder still covers those "
              f"(scripts/record_meridian_flow.py). Backups in {BACKUP}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
