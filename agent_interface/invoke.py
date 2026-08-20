"""
The actual invocation surface behind the catalog: given a capability_id an agent chose and the
typed args it supplied, run the real, deterministic replay engine and return a Result — no LLM
call in this path, same as any other replay.
"""
from __future__ import annotations

from pathlib import Path

from agent_interface.catalog import CAPABILITIES_DIR, load_capabilities
from artifact.schema import Result
from replay.engine import replay


class UnknownCapability(Exception):
    pass


def invoke_capability(
    capability_id: str,
    args: dict,
    confirm: bool = False,
    headless: bool = True,
    capabilities_dir: Path = CAPABILITIES_DIR,
) -> Result:
    """
    `confirm` is a parameter of this function, not of the tool schema an LLM sees
    (catalog.py) -- deliberately not something the calling agent's tool-call input can set. A
    risky capability only executes past its confirmation step if whoever is orchestrating this
    call (a human, or code a human explicitly configured) passes confirm=True; an LLM choosing
    to call the tool is not sufficient by itself, matching check_risk_confirmation's existing
    server-side enforcement in guardrails/policy.py.
    """
    capabilities = load_capabilities(capabilities_dir)
    if capability_id not in capabilities:
        raise UnknownCapability(
            f"{capability_id!r} is not in the catalog (known: {sorted(capabilities)})"
        )
    return replay(capabilities[capability_id], params=args, confirm=confirm, headless=headless)
