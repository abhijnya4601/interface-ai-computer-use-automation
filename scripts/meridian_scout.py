"""
Scout a MERIDIAN CORE member for demo-safe inputs — because the hosted app is stateful in
memory (it only resets on redeploy) and repeated risky-capability runs move balances and place
holds. Prints, per member, the shares that are usable RIGHT NOW:

  - open_shares    : OPEN shares with a positive balance   -> safe `from_share` for a transfer
  - any_open       : all OPEN shares                       -> safe `to_share`
  - holdable       : shares the Place Hold form still lists -> safe `share` for a hold

    python scripts/meridian_scout.py                 # all seed members
    python scripts/meridian_scout.py 100234 103001   # specific members
    python scripts/meridian_scout.py --json 100234   # machine-readable

Needs MERIDIAN_OPERATOR / MERIDIAN_PASSWORD (+ MERIDIAN_SUPERVISOR_* for the holdable list).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

from agent.session import MissingCredentials, credentials_for, load_signon_capability
from replay.engine import replay

BASE = "https://web-sample.interface-hiring.com"
SEED = ["100234", "100987", "101555", "102777", "103001"]
_MONEY = re.compile(r"\$([\d,]+\.\d\d)")


def scout(page, member_id: str) -> dict:
    page.goto(f"{BASE}/members/{member_id}", timeout=20000)
    table = page.locator("xpath=//font[contains(.,'SHARES')]/following::table[1]")
    rows = table.locator("xpath=.//tr[td]")
    open_shares, any_open = [], []
    for i in range(rows.count()):
        cells = [c.strip() for c in rows.nth(i).locator("td").all_text_contents()]
        if len(cells) < 4:
            continue
        sid, _typ, bal, status = cells[0], cells[1], cells[2], cells[3]
        if "OPEN" in status:
            any_open.append(sid)
            m = _MONEY.search(bal)
            if m and float(m.group(1).replace(",", "")) > 0:
                open_shares.append(sid)
    holdable: list[str] = []
    try:
        page.goto(f"{BASE}/members/{member_id}/hold", timeout=20000)
        holdable = [v for v in page.locator("select[name=share] option").evaluate_all(
            "els => els.map(e => e.value)") if v]
    except Exception:
        pass
    return {"member_id": member_id, "open_shares": open_shares[:5],
            "any_open": any_open[:5], "holdable": holdable[:5]}


def main() -> int:
    args = sys.argv[1:]
    as_json = "--json" in args
    members = [a for a in args if a != "--json"] or SEED
    try:
        creds = credentials_for("supervisor")  # supervisor can see the hold form for everyone
    except MissingCredentials:
        creds = credentials_for("teller")

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page()
        replay(load_signon_capability(), params=creds, page=pg, run_id="scout")
        results = [scout(pg, m) for m in members]
        b.close()

    if as_json:
        print(json.dumps(results, indent=2))
        return 0
    print(f"{'member':10} {'from_share (OPEN, >$0)':24} {'to_share (OPEN)':22} holdable share")
    for r in results:
        print(f"{r['member_id']:10} {(r['open_shares'][0] if r['open_shares'] else '—'):24} "
              f"{(r['any_open'][1] if len(r['any_open']) > 1 else (r['any_open'][0] if r['any_open'] else '—')):22} "
              f"{(r['holdable'][0] if r['holdable'] else '— (all held)')}")
    print("\nUse the first two for a transfer, the last for a hold. Values change as the app is "
          "hammered on; it resets on redeploy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
