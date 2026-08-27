"""
Minimal chatbot — the conversational front door standing in for the AI agent. It turns a
request into the right capability invocation(s) against the API and reports the structured
result (or the business outcome / escalation) in plain language. Thin on purpose: a demo driver
over the API, not a second product.

    # terminal 1
    python -m api
    # terminal 2
    export ANTHROPIC_API_KEY=...  MERIDIAN_OPERATOR=teller1 MERIDIAN_PASSWORD=password
    python -m chatbot.cli
    you> what's the first share balance for member 100987?
    you> transfer 1.00 from 100234-MMKT-14 to 100234-MMKT-15 for member 100234

One Claude call per turn (tools only), then one call to phrase the result. No chain-of-thought
is shown. Ctrl-D to quit.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

import anthropic

API = os.environ.get("CAPABILITY_API", "http://localhost:8000")
MODEL = "claude-sonnet-5"
SYSTEM = (
    "You are a back-office assistant for a credit union. You have tools that each run a "
    "pre-recorded, deterministic UI automation ('capability') against the servicing console and "
    "return a structured result. Choose the right tool and typed arguments for the user's "
    "request. If no tool fits, say so plainly — do not guess. After a tool result, answer the "
    "user in one or two sentences, stating the concrete outcome: the value(s) returned, or the "
    "business outcome (e.g. 'no such member', 'source share is on hold', 'insufficient funds'), "
    "or that the action was routed to a human for approval and what happened. Never claim "
    "success unless the tool result status is 'success'."
)


def _get(path: str):
    with urllib.request.urlopen(f"{API}{path}", timeout=30) as r:
        return json.load(r)


def _post(path: str, body: dict):
    req = urllib.request.Request(
        f"{API}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}") or {"error": str(e)}


def _tools_for_claude(catalog: list[dict]) -> list[dict]:
    # the API enriches the catalog with risk_level/target/etc; Claude's tools param wants only
    # name/description/input_schema
    return [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
            for t in catalog]


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("set ANTHROPIC_API_KEY")
    try:
        catalog = _get("/api/capabilities")
    except Exception as exc:
        sys.exit(f"cannot reach the capability API at {API} ({exc}). Start it with `python -m api`.")
    tools = _tools_for_claude(catalog)
    client = anthropic.Anthropic()
    print(f"chatbot ready — {len(tools)} capabilities. Ctrl-D to quit.\n")

    messages: list[dict] = []
    while True:
        try:
            user = input("you> ").strip()
        except EOFError:
            print()
            return 0
        if not user:
            continue
        messages.append({"role": "user", "content": user})

        resp = client.messages.create(model=MODEL, max_tokens=1024, system=SYSTEM,
                                      tools=tools, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            text = "".join(b.text for b in resp.content if b.type == "text")
            print(f"bot> {text.strip() or '(no answer)'}\n")
            continue

        results = []
        for tu in tool_uses:
            print(f"  · invoking {tu.name}({json.dumps(tu.input)}) …")
            payload = _post(f"/api/capabilities/{tu.name}/invoke", {"args": tu.input})
            result = payload.get("result", payload)
            print(f"    -> status={result.get('status')} "
                  f"{result.get('business_outcome_code') or ''} "
                  f"outputs={result.get('outputs') or {}}")
            results.append({"type": "tool_result", "tool_use_id": tu.id,
                            "content": json.dumps(result)})
        messages.append({"role": "user", "content": results})

        follow = client.messages.create(model=MODEL, max_tokens=512, system=SYSTEM,
                                        tools=tools, messages=messages)
        messages.append({"role": "assistant", "content": follow.content})
        text = "".join(b.text for b in follow.content if b.type == "text")
        print(f"bot> {text.strip() or '(done)'}\n")


if __name__ == "__main__":
    sys.exit(main())
