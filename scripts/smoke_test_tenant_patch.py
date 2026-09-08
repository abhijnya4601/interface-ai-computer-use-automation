"""
Live proof of base + per-tenant patch (artifact/patch.py).

Runs the mock bank as the 'acme' tenant (BANK_VARIANT=acme relabels the balance row header),
then:
  1. replays the BASE lookup_member_balance capability -> hard_failure (the row header it
     looks for, 'Savings Balance', doesn't exist on this tenant's UI)
  2. applies capabilities/tenants/acme.lookup_member_balance.patch.json and replays that
     -> success, same member, same everything else

Run:
    BANK_VARIANT=acme python -c "import sys; sys.path.insert(0,'app'); import models; models.init_db(); models.seed()"
    BANK_VARIANT=acme python app/app.py    # in another terminal
    python scripts/smoke_test_tenant_patch.py
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from artifact.patch import apply_patch, patch_summary
from artifact.schema import Capability
from replay.engine import replay

ROOT = Path(__file__).parent.parent
BASE = ROOT / "capabilities" / "lookup_member_balance.v1.json"
PATCH = ROOT / "capabilities" / "tenants" / "acme.lookup_member_balance.patch.json"


def check(label: str, cond: bool):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(f"failed: {label}")


def main():
    if os.environ.get("BANK_VARIANT") != "acme":
        print("note: run the mock app with BANK_VARIANT=acme for this to be meaningful")

    base = Capability.model_validate_json(BASE.read_text())
    patch = json.loads(PATCH.read_text())
    print(f"patch changes: {patch_summary(base, patch)}\n")

    print("=== base capability against the ACME tenant ===")
    r1 = replay(base, {"member_id": "23456"})
    print(r1.model_dump())
    check("base capability hard-fails on the renamed UI", r1.status == "hard_failure")

    print("\n=== base + acme patch against the ACME tenant ===")
    acme = apply_patch(base, patch)
    r2 = replay(acme, {"member_id": "23456"})
    print(r2.model_dump())
    check("patched capability succeeds", r2.status == "success")
    check("and returns the real balance", bool(r2.outputs.get("savings_balance")))

    print("\nbase + patch reuse verified live.")


if __name__ == "__main__":
    main()
