"""
Offline tests for the agent-facing capability interface (stretch goal) -- catalog.py's mapping
from real Capability files to Claude tool-use shapes, and invoke.py's routing/safety behavior.
No browser or API key needed; replay() itself is monkeypatched where a real one would launch
Chromium.
"""
import pytest

from agent_interface.catalog import _generalize_description, build_tool_catalog, load_capabilities
from agent_interface.invoke import UnknownCapability, invoke_capability
from artifact.schema import Capability, Checkpoint, LocatorTarget, Step


def _write_capability(tmp_path, capability_id, risk_level="safe", description="test capability"):
    cap = Capability(
        capability_id=capability_id,
        version="1.0.0",
        created_from_run_id="run_test",
        description=description,
        target={"app_name": "mock-core-banking", "entry_point": "http://x/search"},
        risk_level=risk_level,
        input_schema={"member_id": {"type": "string", "description": "the member id"}},
        output_schema={"balance": {"type": "string"}},
        checkpoint=Checkpoint(type="url_match", expected="/done"),
        steps=[
            Step(step_id="s1", action_type="navigate", value="http://x/search"),
            Step(step_id="s2", action_type="extract",
                 target=LocatorTarget(strategy="role_name", primary={"role": "cell", "name": "x"},
                                       fallbacks=[], reasoning="test"),
                 extract_as="balance"),
        ],
    )
    path = tmp_path / f"{capability_id}.v1.json"
    path.write_text(cap.model_dump_json())
    return cap


# ---- catalog.py ---------------------------------------------------------------------------

def test_build_tool_catalog_maps_input_schema_directly_to_properties(tmp_path):
    _write_capability(tmp_path, "lookup_member_balance", description="Look up a member's balance.")
    tools = build_tool_catalog(tmp_path)

    assert len(tools) == 1
    tool = tools[0]
    assert tool["name"] == "lookup_member_balance"
    assert tool["description"] == "Look up a member's balance."
    assert tool["input_schema"]["type"] == "object"
    assert tool["input_schema"]["properties"] == {
        "member_id": {"type": "string", "description": "the member id"}
    }
    assert tool["input_schema"]["required"] == ["member_id"]


def test_build_tool_catalog_flags_risky_capabilities_in_the_description(tmp_path):
    """A risky capability's tool description must make the risk visible to whatever's reading
    the catalog -- but see the next test: it must NOT make confirm=True settable by the model."""
    _write_capability(tmp_path, "open_subaccount", risk_level="risky",
                       description="Open a sub-account.")
    tools = build_tool_catalog(tmp_path)
    assert "risky" in tools[0]["description"].lower()
    assert "confirmation" in tools[0]["description"].lower()


def test_build_tool_catalog_never_exposes_confirm_as_a_tool_parameter(tmp_path):
    """The core safety property of this interface: an LLM choosing to call a risky capability
    must never be sufficient by itself to execute it. If `confirm` ever leaked into the tool's
    input_schema, a model could just set it to true itself and silently bypass
    check_risk_confirmation -- see invoke.py's docstring for the matching enforcement point."""
    _write_capability(tmp_path, "open_subaccount", risk_level="risky")
    tools = build_tool_catalog(tmp_path)
    assert "confirm" not in tools[0]["input_schema"]["properties"]


def test_build_tool_catalog_falls_back_to_a_placeholder_for_missing_description(tmp_path):
    _write_capability(tmp_path, "some_capability", description="")
    tools = build_tool_catalog(tmp_path)
    assert tools[0]["description"]  # never empty -- always something a reader can act on
    assert "some_capability" in tools[0]["description"]


def test_build_tool_catalog_generalizes_a_hardcoded_example_member_id(tmp_path):
    """D27: found live -- Claude read a tool description literally saying "member 12345" as that
    capability being hardcoded to member 12345, and declined to call it for a different
    member_id it was actually asked about, even though input_schema clearly declares member_id
    as a parameter. The description an agent sees must not contain the specific example ID the
    capability happened to be recorded from."""
    _write_capability(tmp_path, "lookup_member_balance",
                       description="Look up member 12345 and read their current savings balance.")
    tools = build_tool_catalog(tmp_path)
    assert "12345" not in tools[0]["description"]
    assert "member_id" in tools[0]["description"]


def test_generalize_description_leaves_non_member_id_capabilities_untouched():
    """Only rewrites when member_id is actually a declared input -- a capability that happens to
    mention "member 12345" for an unrelated reason isn't silently mangled."""
    desc = "Some capability that mentions member 12345 for another reason."
    assert _generalize_description(desc, {"other_param": {"type": "string"}}) == desc


def test_load_capabilities_keys_by_capability_id(tmp_path):
    _write_capability(tmp_path, "lookup_member_balance")
    _write_capability(tmp_path, "open_subaccount", risk_level="risky")
    caps = load_capabilities(tmp_path)
    assert set(caps) == {"lookup_member_balance", "open_subaccount"}
    assert caps["open_subaccount"].risk_level == "risky"


# ---- invoke.py ------------------------------------------------------------------------------

def test_invoke_capability_raises_for_an_unknown_name(tmp_path):
    _write_capability(tmp_path, "lookup_member_balance")
    with pytest.raises(UnknownCapability):
        invoke_capability("does_not_exist", {"member_id": "1"}, capabilities_dir=tmp_path)


def test_invoke_capability_routes_through_the_real_replay_function(tmp_path, monkeypatch):
    """Doesn't launch a real browser -- monkeypatches replay() itself and asserts invoke_capability
    resolved the right Capability object and forwarded args/confirm/headless correctly, which is
    the actual routing logic this module owns (replay()'s own behavior is replay/engine.py's to
    test, not duplicated here)."""
    _write_capability(tmp_path, "open_subaccount", risk_level="risky")
    calls = []

    def fake_replay(capability, params, confirm=False, headless=True, run_id=None):
        calls.append((capability.capability_id, params, confirm, headless))
        from artifact.schema import Result
        return Result(status="success", outputs={})

    import agent_interface.invoke as invoke_module
    monkeypatch.setattr(invoke_module, "replay", fake_replay)

    result = invoke_capability(
        "open_subaccount", {"member_id": "56789"}, confirm=True, headless=False,
        capabilities_dir=tmp_path,
    )
    assert result.status == "success"
    assert calls == [("open_subaccount", {"member_id": "56789"}, True, False)]


def test_invoke_capability_defaults_confirm_to_false(tmp_path, monkeypatch):
    """A caller that forgets to pass confirm must NOT accidentally execute a risky capability --
    the default has to be the safe one."""
    _write_capability(tmp_path, "open_subaccount", risk_level="risky")
    calls = []

    def fake_replay(capability, params, confirm=False, headless=True, run_id=None):
        calls.append(confirm)
        from artifact.schema import Result
        return Result(status="hard_failure")

    import agent_interface.invoke as invoke_module
    monkeypatch.setattr(invoke_module, "replay", fake_replay)

    invoke_capability("open_subaccount", {"member_id": "1"}, capabilities_dir=tmp_path)
    assert calls == [False]
