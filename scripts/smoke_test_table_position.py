"""
Live smoke test for the table_position locator strategy (D22, fixing D21's real find). Uses
`page.set_content()` with two versions of the same table shape (different data, same structure)
so this doesn't need the Flask app running — a real browser is enough. Proves the exact scenario
that failed in D21: build a locator against one row's data, then successfully resolve the
*analogous* cell on a page with completely different data (not the old value).

Run: python scripts/smoke_test_table_position.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

from agent.recorder import Recorder
from replay.engine import _locate_table_position

TABLE_A = """
<table>
  <thead><tr><th scope="col">Date</th><th scope="col">Description</th><th scope="col">Amount</th></tr></thead>
  <tbody>
    <tr><td>2026-08-15</td><td>Grocery Store Purchase</td><td>-45.23</td></tr>
    <tr><td>2026-08-10</td><td>Payroll Deposit</td><td>2500.00</td></tr>
  </tbody>
</table>
"""

TABLE_B = """
<table>
  <thead><tr><th scope="col">Date</th><th scope="col">Description</th><th scope="col">Amount</th></tr></thead>
  <tbody>
    <tr><td>2026-08-12</td><td>Online Transfer Out</td><td>-50.00</td></tr>
    <tr><td>2026-08-01</td><td>Payroll Deposit</td><td>800.00</td></tr>
  </tbody>
</table>
"""


def check(label: str, condition: bool):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"Smoke test failed: {label}")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # --- record against TABLE_A: extract the top-left data cell (the "date" of row 0) ---
        page.set_content(TABLE_A)
        recorder = Recorder(goal="read the latest transaction")
        target = recorder._try_table_position_locator("cell", "2026-08-15", page)

        check("table_position locator was built (not None)", target is not None)
        check("strategy is table_position", target.strategy == "table_position")
        check("row_index is 0 (the first/latest data row)", target.primary["row_index"] == 0)
        check("column_index is 0 (the Date column)", target.primary["column_index"] == 0)
        check("table_headers captured correctly",
              target.primary["table_headers"] == ["Date", "Description", "Amount"])
        check("reasoning explains the position-based choice",
              "position" in target.reasoning.lower())
        print(f"\nlocator built from TABLE_A: {target.primary}\n")

        # --- replay against TABLE_B: completely different data, same structure ---
        page.set_content(TABLE_B)
        resolved = _locate_table_position(page, target.primary)
        check("locator resolves on a page with totally different data", resolved is not None)
        resolved_text = resolved.text_content() if resolved else None
        check(f"resolved to TABLE_B's actual latest date ('2026-08-12'), not TABLE_A's stale value",
              resolved_text == "2026-08-12")
        print(f"resolved cell text on TABLE_B: {resolved_text!r} (this IS the D21 fix)\n")

        # --- also confirm a labeled row (th scope=row) is correctly NOT treated as table_position ---
        page.set_content("""
            <table><tbody>
              <tr><th scope="row">Savings Balance</th><td>$1,842.30</td></tr>
            </tbody></table>
        """)
        labeled_target = recorder._try_table_position_locator("cell", "$1,842.30", page)
        check("a labeled th/td row is NOT converted to table_position (existing tier still wins)",
              labeled_target is None)

        browser.close()

    print("\nAll table_position smoke checks passed.")


if __name__ == "__main__":
    main()
