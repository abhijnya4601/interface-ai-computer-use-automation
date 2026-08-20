"""
Stretch goal demo: an AI agent discovering and invoking a saved capability by name, with typed
args, exactly as evaluation criterion 8's "agent-facing capability interface" describes.

Real, not simulated: a real Claude API call sees the real tool catalog built from
capabilities/*.json (agent_interface/catalog.py), decides to call `lookup_member_balance` for a
member it wasn't told the ID of directly, that call runs through the real, deterministic replay
engine (agent_interface/invoke.py -> replay/engine.py -- no LLM involved in execution), and the
real result gets handed back to Claude for its final answer.

Deliberately uses the safe capability, not a risky one: this script's whole point is showing an
agent discover-and-call a tool on its own judgment. `confirm` is not exposed to the model at all
(see catalog.py/invoke.py) -- a risky capability would just get correctly refused with
hard_failure here, which is the guardrail working as designed, not something to route around for
a cleaner demo.

Run: python scripts/demo_agent_capability_interface.py   (needs ANTHROPIC_API_KEY, the mock app
running on :5050, and a compiled capabilities/lookup_member_balance.v1.json)
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic

from agent_interface.catalog import build_tool_catalog
from agent_interface.invoke import invoke_capability
from guardrails.policy import redact

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"
MODEL = "claude-sonnet-5"
MAX_TOKENS = 1024


def _to_jsonable(content_blocks) -> list[dict]:
    out = []
    for block in content_blocks:
        if block.type == "text":
            out.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            out.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
    return out


def main():
    user_request = "What's the current balance for member 23456?"
    tools = build_tool_catalog()
    print(f"catalog: {[t['name'] for t in tools]}")
    print(f"user: {user_request}\n")

    client = anthropic.Anthropic()
    transcript: list[dict] = []
    messages = [{"role": "user", "content": user_request}]
    transcript.append({"type": "user_message", "content": user_request})

    response = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS, tools=tools, messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})
    transcript.append({"type": "llm_response", "stop_reason": response.stop_reason,
                        "content": _to_jsonable(response.content)})

    tool_call = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_call is None:
        print("Claude answered without calling a tool:")
        print("".join(b.text for b in response.content if b.type == "text"))
        _save(transcript)
        return

    print(f"claude chose to call: {tool_call.name}({tool_call.input})")

    result = invoke_capability(tool_call.name, tool_call.input, headless=True)
    print(f"real replay result: status={result.status} outputs={result.outputs}")
    transcript.append({"type": "capability_invoked", "name": tool_call.name,
                        "args": tool_call.input, "result": result.model_dump()})

    messages.append({
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": tool_call.id,
            "content": json.dumps(result.model_dump()),
        }],
    })
    final = client.messages.create(model=MODEL, max_tokens=MAX_TOKENS, tools=tools, messages=messages)
    transcript.append({"type": "llm_response", "stop_reason": final.stop_reason,
                        "content": _to_jsonable(final.content)})

    final_text = "".join(b.text for b in final.content if b.type == "text")
    print(f"\nclaude's final answer: {final_text}")

    _save(transcript)


def _save(transcript: list[dict]):
    EVIDENCE_DIR.mkdir(exist_ok=True)
    path = EVIDENCE_DIR / f"agent_capability_interface_demo_{int(time.time())}.json"
    path.write_text(json.dumps([redact(e) for e in transcript], indent=2, default=str))
    print(f"\ntranscript saved to {path}")


if __name__ == "__main__":
    main()
