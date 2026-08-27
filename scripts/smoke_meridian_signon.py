"""
Live smoke test for the legacy-form adapter (agent/legacy_locate.py) against the real MERIDIAN
CORE target — no LLM, no API key. Proves the Phase-A gate end to end:

  1. perception.build_observation() gives MERIDIAN's label-less <input>/<select> controls a
     usable name (derived from the visible label cell next to them),
  2. the recorder turns a type/click on one of those into a `labeled_field` Step with a
     `field_name` fallback,
  3. executing those steps actually logs in and reaches /menu,
  4. compiling them into a Capability and replaying it deterministically (replay/engine.py,
     no LLM) reaches /menu again.

Run:  python scripts/smoke_meridian_signon.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

from agent.compiler import compile_capability
from agent.perception import _find_nodes_by_role, build_observation
from agent.recorder import Recorder
from agent.tools import execute_click, execute_type
from artifact.schema import Checkpoint
from replay.engine import replay

TARGET = "https://web-sample.interface-hiring.com/signon"
OPERATOR = os.environ.get("MERIDIAN_OPERATOR", "teller1")
PASSWORD = os.environ.get("MERIDIAN_PASSWORD", "password")
BRANCH = os.environ.get("MERIDIAN_BRANCH", "MAIN-001")

_checks: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    _checks.append((label, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(TARGET, timeout=20000)

        # 1. perception enriches the label-less controls
        obs = build_observation(page)
        tree = obs["accessibility_tree"]
        names = {n.get("name") for n in _find_nodes_by_role(tree, "textbox")}
        names |= {n.get("name") for n in _find_nodes_by_role(tree, "combobox")}
        check("perception derived a name for the Operator ID field", "Operator ID" in names)
        check("perception derived a name for the Password field", "Password" in names)
        check("perception derived a name for the Branch field", "Branch" in names)

        # 2. recorder builds labeled_field Steps (recorded on the signon page, before executing)
        rec = Recorder(goal=f"Sign on as {OPERATOR}")
        rec.record_navigate(TARGET)
        rec.record_type("textbox", "Operator ID", OPERATOR, page)
        rec.record_type("textbox", "Password", PASSWORD, page)
        rec.record_type("combobox", "Branch", BRANCH, page)
        rec.record_click("button", "Sign On", page)

        op_step = rec.steps[1]  # steps[0] is the navigate
        check("Operator ID step recorded as strategy=labeled_field",
              op_step.target.strategy == "labeled_field")
        check("labeled_field step carries a field_name fallback (name='operator')",
              any(fb.get("strategy") == "field_name" and fb.get("name") == "operator"
                  for fb in op_step.target.fallbacks))
        check("Sign On button still resolves via role_name (buttons have accessible names)",
              rec.steps[-1].target.strategy == "role_name")

        # 3. executing the recorded actions actually logs in
        execute_type(page, "textbox", "Operator ID", OPERATOR)
        execute_type(page, "textbox", "Password", PASSWORD)
        execute_type(page, "combobox", "Branch", BRANCH)
        execute_click(page, "button", "Sign On")
        page.wait_for_load_state()
        check("executing the recorded steps reached /menu", page.url.rstrip("/").endswith("/menu"))

        browser.close()

    # 4. compile + deterministic replay — outside the sync_playwright() block above, since
    # replay() opens its own.
    capability = compile_capability(
        capability_id="meridian_signon_smoke",
        version="1.0.0",
        run_id="smoke",
        target_url=TARGET,
        risk_level="safe",
        recorder=rec,
        outputs={},
        checkpoint=Checkpoint(type="url_match", locator=None, expected="menu"),
        surface_type="legacy_web",
        description=f"Sign on to MERIDIAN CORE as {OPERATOR}",
    )
    result = replay(capability, params={}, headless=True)
    check(f"deterministic replay status == success (got {result.status})",
          result.status == "success")

    failed = [label for label, ok in _checks if not ok]
    print(f"\n{'ALL PASS' if not failed else f'{len(failed)} FAILED'}  "
          f"({sum(ok for _, ok in _checks)}/{len(_checks)})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
