"""
Phase 1 live check — the "verify the sensor before building the brain that reads it" gate.
Launches a real Chromium against the real Flask app (both must be running — see README),
drives through the sub-account flow up to the iframe-based confirmation screen, and asserts the
observation build_observation() produces actually contains what the discovery agent will need.

Run: python scripts/verify_perception_live.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

from agent.perception import _find_nodes_by_role, build_observation

BASE = "http://localhost:5050"


def check(label: str, condition: bool):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"Live perception check failed: {label}")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # --- /search, before any query ---
        page.goto(f"{BASE}/search")
        obs = build_observation(page, last_action_result="navigated to /search")
        print("\n=== observation: /search ===")
        print(json.dumps(obs, indent=2))
        buttons = _find_nodes_by_role(obs["accessibility_tree"], "button")
        check('role:"button" name:"Go" present after loading /search',
              any(b.get("name") == "Go" for b in buttons))

        # --- submit a search ---
        page.get_by_label("Search (ID / name)").fill("12345")
        page.get_by_role("button", name="Go").click()
        page.wait_for_load_state("networkidle")
        obs = build_observation(page, last_action_result="clicked 'Go' — page changed")
        print("\n=== observation: /search results ===")
        print(json.dumps(obs, indent=2)[:2000])
        tables_or_rows = _find_nodes_by_role(obs["accessibility_tree"], "table") + \
            _find_nodes_by_role(obs["accessibility_tree"], "row") + \
            _find_nodes_by_role(obs["accessibility_tree"], "cell")
        check('role:"table" (or row/cell roles) present after submitting a search',
              len(tables_or_rows) > 0)

        # --- drive to the iframe-based confirmation screen ---
        page.goto(f"{BASE}/member/12345/new-subaccount")
        page.get_by_label("Account Type").select_option("christmas_club")
        page.get_by_label("Nickname").fill("Holiday")
        page.get_by_label("Opening Deposit ($)").fill("25")
        page.get_by_role("button", name="Continue").click()
        page.wait_for_load_state("networkidle")

        obs = build_observation(page, last_action_result="clicked 'Continue' — page changed")
        print("\n=== observation: confirm_wrapper (with synthetic Iframe node) ===")
        print(json.dumps(obs, indent=2))

        iframe_nodes = _find_nodes_by_role(obs["accessibility_tree"], "Iframe")
        check('a synthetic {role: "Iframe", ...} node is present on the confirm-wrapper page',
              len(iframe_nodes) == 1)

        confirm_buttons = _find_nodes_by_role(obs["accessibility_tree"], "button")
        check('"Confirm and Open Account" (inside the iframe) is reachable through the '
              "synthetic Iframe node",
              any(b.get("name") == "Confirm and Open Account" for b in confirm_buttons))

        browser.close()
        print("\nAll live perception checks passed.")


if __name__ == "__main__":
    main()
