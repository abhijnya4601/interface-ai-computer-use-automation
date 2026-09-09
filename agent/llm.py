"""
Provider-agnostic backend for the discovery tool-use loop.

Discovery needs very little from a model: send a system prompt + a growing transcript + a
fixed set of tools, get back either text or a tool call, feed the tool's result back, repeat
(agent/discovery.py). Three providers implement exactly that here — Anthropic (Claude),
OpenAI (GPT), and Google (Gemini) — behind one small `_Session` interface, so whoever is
driving the console can bring whichever key they already have.

The tool *schemas* are authored once, in Anthropic form (agent/tools.py); each session
translates them to its provider's function-calling shape. Each session also owns its own
conversation history in the provider's native format — the loop never sees it.

SDKs for OpenAI and Gemini are imported lazily inside their session classes, so a
replay-only or Anthropic-only deployment doesn't need them installed.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from agent.tools import TOOLS

DEFAULT_MODELS = {
    "anthropic": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
    "openai": os.environ.get("OPENAI_MODEL", "gpt-4o"),
    "gemini": os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
}


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Turn:
    """One model response, normalised across providers."""
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: dict  # {"input": int, "output": int}

    def jsonable(self) -> list[dict]:
        """The shape agent/discovery.py records in its transcript (and the console renders)."""
        out: list[dict] = []
        if self.text:
            out.append({"type": "text", "text": self.text})
        for c in self.tool_calls:
            out.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.input})
        return out


def detect_provider(api_key: str, explicit: str | None = None) -> str:
    """Which provider a key belongs to. `explicit` (anything but None/"auto") wins outright."""
    if explicit and explicit not in ("auto", ""):
        if explicit not in DEFAULT_MODELS:
            raise ValueError(f"unknown provider {explicit!r} — anthropic / openai / gemini")
        return explicit
    k = (api_key or "").strip()
    if k.startswith("sk-ant-"):
        return "anthropic"
    if k.startswith("AIza"):
        return "gemini"
    if k.startswith("sk-"):  # sk-, sk-proj-, ...
        return "openai"
    raise ValueError(
        "couldn't tell which provider this key is for — pick one explicitly "
        "(keys normally start sk-ant- for Anthropic, sk- for OpenAI, AIza for Google)"
    )


def make_session(api_key: str | None = None, provider: str | None = None,
                 model: str | None = None, tools: list[dict] | None = None) -> _Session:
    """Build the right session. With neither key nor provider, defaults to Anthropic reading
    ANTHROPIC_API_KEY from the environment (the historical behaviour).

    `tools` is a list of Anthropic-schema tool dicts (`{name, description, input_schema}`);
    defaults to the discovery action tools (agent/tools.py). The console's Ask mode passes the
    capability catalog instead."""
    prov = "anthropic"
    if api_key or (provider and provider not in ("auto", "")):
        prov = detect_provider(api_key or "", provider)
    model = model or DEFAULT_MODELS[prov]
    tools = tools or TOOLS
    if prov == "anthropic":
        return _AnthropicSession(api_key, model, tools)
    if prov == "openai":
        return _OpenAISession(api_key, model, tools)
    return _GeminiSession(api_key, model, tools)


# --------------------------------------------------------------------------------------------
# One interface, three implementations. Each keeps `self.provider`, `self.model`,
# `self.retry_exceptions`, and:
#   add_user_text(text)            -> append a user turn (an observation, JSON-encoded)
#   complete(system, max_tokens)   -> call the model, append the assistant turn, return a Turn
#   add_tool_results([(id, str)])  -> append the tool results for the turn just returned
# --------------------------------------------------------------------------------------------

class _Session:
    provider: str
    model: str
    retry_exceptions: tuple

    def add_user_text(self, text: str) -> None: ...
    def complete(self, system: str, max_tokens: int, force_tool: bool = True) -> Turn: ...
    def add_tool_results(self, results: list[tuple[str, str]]) -> None: ...


class _AnthropicSession(_Session):
    provider = "anthropic"

    def __init__(self, api_key: str | None, model: str, tools: list[dict]):
        import anthropic
        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.retry_exceptions = (anthropic.APIConnectionError, anthropic.InternalServerError,
                                 anthropic.RateLimitError)
        self._messages: list[dict] = []
        self._tools = [{"name": t["name"], "description": t["description"],
                        "input_schema": t["input_schema"]} for t in tools]

    def add_user_text(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

    def complete(self, system: str, max_tokens: int, force_tool: bool = True) -> Turn:
        r = self._client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system, tools=self._tools,
            tool_choice={"type": "any" if force_tool else "auto"}, messages=self._messages,
        )
        self._messages.append({"role": "assistant", "content": r.content})
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in r.content if b.type == "tool_use"]
        text = "".join(b.text for b in r.content if b.type == "text")
        u = getattr(r, "usage", None)
        usage = {"input": getattr(u, "input_tokens", 0), "output": getattr(u, "output_tokens", 0)} \
            if u is not None else {"input": 0, "output": 0}
        return Turn(text, calls, r.stop_reason or "", usage)

    def add_tool_results(self, results: list[tuple[str, str]]) -> None:
        self._messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": cid, "content": content}
            for cid, content in results
        ]})


class _OpenAISession(_Session):
    provider = "openai"

    def __init__(self, api_key: str | None, model: str, tools: list[dict]):
        import openai
        self.model = model
        self._client = openai.OpenAI(api_key=api_key) if api_key else openai.OpenAI()
        self.retry_exceptions = tuple(c for c in (
            getattr(openai, "APIConnectionError", None), getattr(openai, "APITimeoutError", None),
            getattr(openai, "InternalServerError", None), getattr(openai, "RateLimitError", None),
        ) if isinstance(c, type))
        self._messages: list[dict] = []
        self._tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"],
        }} for t in tools]

    def add_user_text(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

    def complete(self, system: str, max_tokens: int, force_tool: bool = True) -> Turn:
        r = self._client.chat.completions.create(
            model=self.model, max_tokens=max_tokens, tools=self._tools,
            tool_choice="required" if force_tool else "auto",
            messages=[{"role": "system", "content": system}, *self._messages],
        )
        m = r.choices[0].message
        calls = []
        for tc in (m.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        assistant: dict = {"role": "assistant", "content": m.content or ""}
        if m.tool_calls:
            assistant["tool_calls"] = [{
                "id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
            } for tc in m.tool_calls]
        self._messages.append(assistant)
        u = getattr(r, "usage", None)
        usage = {"input": getattr(u, "prompt_tokens", 0), "output": getattr(u, "completion_tokens", 0)} \
            if u is not None else {"input": 0, "output": 0}
        return Turn(m.content or "", calls, r.choices[0].finish_reason or "", usage)

    def add_tool_results(self, results: list[tuple[str, str]]) -> None:
        for cid, content in results:
            self._messages.append({"role": "tool", "tool_call_id": cid, "content": content})


class _GeminiSession(_Session):
    provider = "gemini"

    def __init__(self, api_key: str | None, model: str, tools: list[dict]):
        from google import genai
        from google.genai import errors as gerr
        from google.genai import types
        self._genai, self._types = genai, types
        self.model = model
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self._client = genai.Client(api_key=key) if key else genai.Client()
        self.retry_exceptions = tuple(c for c in (
            getattr(gerr, "ServerError", None), getattr(gerr, "APIError", None),
        ) if isinstance(c, type))
        self._contents: list = []
        self._pending: dict[str, str] = {}  # our synthetic call id -> function name (Gemini has no ids)
        self._tools = [types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=t["name"], description=t["description"],
                parameters=_gemini_schema(t["input_schema"]),
            ) for t in tools
        ])]

    def add_user_text(self, text: str) -> None:
        t = self._types
        self._contents.append(t.Content(role="user", parts=[t.Part(text=text)]))

    def complete(self, system: str, max_tokens: int, force_tool: bool = True) -> Turn:
        t = self._types
        cfg = t.GenerateContentConfig(
            system_instruction=system, tools=self._tools, max_output_tokens=max_tokens,
            tool_config=t.ToolConfig(function_calling_config=t.FunctionCallingConfig(
                mode="ANY" if force_tool else "AUTO")),
        )
        r = self._client.models.generate_content(model=self.model, contents=self._contents, config=cfg)
        cand = r.candidates[0]
        self._contents.append(cand.content)
        calls, text, self._pending = [], "", {}
        for i, part in enumerate(cand.content.parts or []):
            fc = getattr(part, "function_call", None)
            if fc is not None:
                cid = f"{fc.name}-{i}"
                self._pending[cid] = fc.name
                calls.append(ToolCall(cid, fc.name, dict(fc.args or {})))
            elif getattr(part, "text", None):
                text += part.text
        um = getattr(r, "usage_metadata", None)
        usage = {"input": getattr(um, "prompt_token_count", 0),
                 "output": getattr(um, "candidates_token_count", 0)} if um is not None \
            else {"input": 0, "output": 0}
        return Turn(text, calls, str(getattr(cand, "finish_reason", "") or ""), usage)

    def add_tool_results(self, results: list[tuple[str, str]]) -> None:
        t = self._types
        parts = [t.Part.from_function_response(name=self._pending.get(cid, cid),
                                              response={"result": content})
                 for cid, content in results]
        self._contents.append(t.Content(role="user", parts=parts))


def _gemini_schema(schema: dict) -> dict:
    """Anthropic/JSON-Schema -> the OpenAPI subset Gemini's FunctionDeclaration accepts: keep
    only the recognised keys, recurse into properties/items."""
    keep = ("type", "description", "enum", "required", "nullable", "format")
    out = {k: schema[k] for k in keep if k in schema}
    if "properties" in schema:
        out["properties"] = {k: _gemini_schema(v) for k, v in schema["properties"].items()}
    if "items" in schema:
        out["items"] = _gemini_schema(schema["items"])
    return out
