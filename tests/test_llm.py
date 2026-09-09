"""
agent/llm.py — provider detection, tool-schema translation, and the Turn shape the discovery
loop depends on. The real provider calls need a key and a network, so those are not exercised
here; the wiring that decides *which* provider and *how the tools are shaped* is.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.llm import Turn, _gemini_schema, detect_provider, make_session


@pytest.mark.parametrize("key,expected", [
    ("sk-ant-api03-xxxx", "anthropic"),
    ("sk-xxxx", "openai"),
    ("sk-proj-xxxx", "openai"),
    ("AIzaSyD-xxxx", "gemini"),
])
def test_detect_provider_from_key_prefix(key, expected):
    assert detect_provider(key) == expected


def test_detect_provider_explicit_overrides_the_prefix():
    assert detect_provider("sk-ant-xxxx", "gemini") == "gemini"
    assert detect_provider("", "openai") == "openai"


def test_detect_provider_rejects_an_unrecognisable_key():
    with pytest.raises(ValueError):
        detect_provider("nope-not-a-key")


def test_detect_provider_rejects_an_unknown_explicit_provider():
    with pytest.raises(ValueError):
        detect_provider("sk-ant-xxxx", "cohere")


def test_make_session_defaults_to_anthropic_with_no_key_or_provider():
    s = make_session()
    assert s.provider == "anthropic"
    assert s.model  # a concrete default model id


def test_make_session_picks_the_model_default_for_the_detected_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-test")
    # reload so the module-level DEFAULT_MODELS re-reads the env
    import importlib

    import agent.llm as llm
    importlib.reload(llm)
    assert llm.DEFAULT_MODELS["openai"] == "gpt-4o-test"
    importlib.reload(llm)  # restore


def test_turn_jsonable_matches_the_transcript_shape():
    from agent.llm import ToolCall
    t = Turn(text="thinking", tool_calls=[ToolCall("id1", "click", {"role": "button", "name": "OK"})],
             stop_reason="tool_use", usage={"input": 10, "output": 5})
    js = t.jsonable()
    assert js[0] == {"type": "text", "text": "thinking"}
    assert js[1] == {"type": "tool_use", "id": "id1", "name": "click",
                     "input": {"role": "button", "name": "OK"}}


def test_gemini_schema_strips_keys_gemini_rejects_and_recurses():
    src = {
        "type": "object",
        "properties": {
            "role": {"type": "string", "description": "aria role"},
            "opts": {"type": "array", "items": {"type": "string", "title": "drop me"}},
        },
        "required": ["role"],
        "additionalProperties": False,   # not in Gemini's OpenAPI subset
    }
    out = _gemini_schema(src)
    assert "additionalProperties" not in out
    assert out["required"] == ["role"]
    assert out["properties"]["role"] == {"type": "string", "description": "aria role"}
    assert out["properties"]["opts"]["items"] == {"type": "string"}  # "title" dropped
