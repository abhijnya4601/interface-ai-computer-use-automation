"""
Builds a Claude tool-use catalog directly from the real, compiled capabilities in
/capabilities/ — no separate registry to keep in sync. Each Capability's own `input_schema`
is already `{param_name: {"type": ..., "description": ...}}`, which is exactly the shape a
JSON-Schema `properties` object needs, so the mapping is direct rather than a translation layer
that could drift from what replay() actually accepts.
"""
from __future__ import annotations

from pathlib import Path

from agent.recorder import _MEMBER_ID_RE
from artifact.schema import Capability

CAPABILITIES_DIR = Path(__file__).parent.parent / "capabilities"


def _generalize_description(description: str, input_schema: dict) -> str:
    """
    `Capability.description` is the literal, historical discovery goal (e.g. "Look up member
    12345 and read their current savings balance.") -- exactly right for human/provenance
    review, but actively misleading as a tool description for an LLM choosing how to call this
    capability. Found live: Claude read "member 12345" as this capability being hardcoded to
    that one member and declined to call it for a different member_id, even
    though `input_schema` clearly declares member_id as a required parameter. Reuses the exact
    same regex the recorder uses to detect the parameterized ID in the first place, rather than
    inventing new detection logic, and only rewrites it when member_id is actually a declared
    parameter -- a capability recorded from a goal that happens to mention "member 12345" for
    some other reason wouldn't get silently mangled.
    """
    if "member_id" not in input_schema:
        return description
    return _MEMBER_ID_RE.sub("a member (member_id)", description)


def load_capabilities(capabilities_dir: Path = CAPABILITIES_DIR) -> dict[str, Capability]:
    """capability_id -> Capability, for every *.json file in capabilities_dir."""
    capabilities = {}
    for path in sorted(capabilities_dir.glob("*.json")):
        cap = Capability.model_validate_json(path.read_text())
        capabilities[cap.capability_id] = cap
    return capabilities


def build_tool_catalog(capabilities_dir: Path = CAPABILITIES_DIR) -> list[dict]:
    """
    One Claude tool-use tool per capability. Deliberately does NOT expose `confirm` as a
    tool parameter even for risk_level="risky" capabilities: that gate exists so a human (or
    calling code a human trusts) decides whether an irreversible action proceeds, not the LLM
    choosing to call the tool. Exposing it here would let the model set confirm=True itself and
    silently defeat the same guardrail replay.py already enforces server-side
    (guardrails/policy.py::check_risk_confirmation) -- see invoke.py.
    """
    tools = []
    for cap in load_capabilities(capabilities_dir).values():
        description = cap.description or f"(no description recorded for {cap.capability_id})"
        description = _generalize_description(description, cap.input_schema)
        if cap.risk_level == "risky":
            description += " [risky: state-changing, requires human confirmation to execute]"
        tools.append({
            "name": cap.capability_id,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": cap.input_schema,
                "required": list(cap.input_schema.keys()),
            },
        })
    return tools
