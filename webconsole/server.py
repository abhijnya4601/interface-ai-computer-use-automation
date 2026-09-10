"""
Link-gated live console. One page: choose a target + parameters, hit Run, and watch the agent
console and a live view of the browser side by side.

    python -m webconsole.server            # http://localhost:5055/?key=<printed>

Access: every request needs `?key=<CONSOLE_ACCESS_KEY>` once (then a cookie carries it). If the
env var is unset a random key is generated and the share URL is printed - fail-secure, never
served open. Discovery mode needs ANTHROPIC_API_KEY in the server env; replay mode never does.

Targets are limited to the approved non-prod app in guardrails/allowlist.yaml - pointing an LLM
at an arbitrary site is exactly what that allowlist exists to prevent.
"""
from __future__ import annotations

import json
import os
import secrets
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request

from agent_interface.runs import get_run, list_runs
from escalation import controller
from escalation import policy as esc_policy
from webconsole import runner

app = Flask(__name__)

SECRET = os.environ.get("CONSOLE_ACCESS_KEY") or secrets.token_urlsafe(9)
_GENERATED = "CONSOLE_ACCESS_KEY" not in os.environ
TARGET_BASE = os.environ.get("TARGET_BASE", "http://localhost:5050")
HOSTILE_BASE = os.environ.get("HOSTILE_BASE", "http://localhost:5051")
# ?inject=<kind> fault switch on the hostile-DOM target (app2/). Kept as an allowlist so a
# viewer can't append arbitrary query strings to the entry navigation.
INJECT_KINDS = ("", "maintenance", "slow", "blank")
# Discover mode uses a key from the server env if present, else one the viewer pastes in the UI
# (bring-your-own-key). The pasted key is passed straight to Anthropic and never stored or logged.
# Discovery accepts a key for any of three providers (Anthropic / OpenAI / Google-Gemini),
# from the server env if present, else pasted in the UI (bring-your-own-key). A pasted key is
# passed straight to that provider and never stored or logged.
_SERVER_KEY_ENVS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
SERVER_HAS_KEY = any(os.environ.get(k) for k in _SERVER_KEY_ENVS)
ALLOW_BYO_KEY = os.environ.get("CONSOLE_ALLOW_BYO_KEY", "1") != "0"

TARGETS = {
    "mock-core-banking": {
        "label": "Mock core-banking app (legacy web, semantic HTML)",
        "app_name": "mock-core-banking",
        "entry": TARGET_BASE + "/search",
        "supports_inject": False,
    },
    "hostile-dom": {
        "label": "Hostile-DOM app (div soup, ARIA roles kept) - fault injection",
        "app_name": "hostile-dom",
        "entry": HOSTILE_BASE + "/find",
        "supports_inject": True,
    },
}


def _authed() -> bool:
    return request.args.get("key") == SECRET or request.cookies.get("ck") == SECRET


@app.before_request
def _gate():
    if request.path in ("/health",):
        return None
    if not _authed():
        return Response("This console is link-only. Append ?key=<token> to the URL.", 403)
    return None


@app.after_request
def _setcookie(resp):
    if request.args.get("key") == SECRET and not request.cookies.get("ck"):
        # 30 days: a demo link is shared and revisited over days, not one session
        resp.set_cookie("ck", SECRET, httponly=True, samesite="Lax", max_age=2592000)
    return resp


@app.route("/health")
def health():
    return "ok"


@app.route("/")
def index():
    if request.args.get("key") == SECRET:
        return redirect("/")  # drop the key from the address bar; cookie carries it now
    cat = runner.catalog()
    return render_template(
        "console.html",
        targets=TARGETS,
        capabilities=cat["capabilities"],
        server_has_key=SERVER_HAS_KEY,
        allow_byo_key=ALLOW_BYO_KEY,
        busy=runner.current() is not None and runner.current().status == "running",
    )


@app.route("/catalog")
def catalog():
    """The current capability list - the replay dropdown refreshes from this after a discovery
    run so a just-discovered capability is immediately replayable without a page reload."""
    return jsonify(runner.catalog())


@app.route("/rules", methods=["GET", "POST", "DELETE"])
def rules():
    """The escalation policy (escalation/rules.yaml). GET lists; POST appends/replaces a rule
    (by `id`); DELETE removes `?id=`. First matching rule wins; no match = allow."""
    if request.method == "GET":
        return jsonify(rules=esc_policy.load_rules(), path="escalation/rules.yaml")

    current = esc_policy.load_rules()
    if request.method == "DELETE":
        rid = request.args.get("id")
        esc_policy.save_rules([r for r in current if r.get("id") != rid])
        return jsonify(rules=esc_policy.load_rules())

    body = request.get_json(force=True, silent=True) or {}
    then = body.get("then")
    if then not in ("escalate", "block", "allow"):
        return jsonify(error="`then` must be escalate / block / allow"), 400
    when = {k: v for k, v in (body.get("when") or {}).items() if v not in (None, "", [])}
    if not when:
        return jsonify(error="a rule needs at least one `when` condition"), 400
    rule = {"id": body.get("id") or ("rule-" + uuid.uuid4().hex[:6]),
            "description": (body.get("description") or "").strip() or "custom rule",
            "when": when, "then": then}
    merged = [r for r in current if r.get("id") != rule["id"]] + [rule]
    esc_policy.save_rules(merged)
    return jsonify(rules=esc_policy.load_rules(), added=rule["id"])


def _resolve_model_key(body: dict):
    """(api_key, provider, None) or (None, None, (json_error, status)). Shared by discovery and
    ask: a pasted BYO key wins, else the first server key env that's set."""
    byo = (body.get("api_key") or "").strip() if ALLOW_BYO_KEY else ""
    provider = (body.get("provider") or "auto").strip() or "auto"
    # Only sniff the key shape when the provider is left on auto-detect. If the viewer explicitly
    # picked a provider, trust their key — formats vary (Google now issues both `AIza...` and
    # `AQ.<...>` keys) and the real check is the provider's own API call.
    known_prefixes = ("sk-ant-", "sk-", "AIza", "AQ.")
    if byo and provider == "auto" and not byo.startswith(known_prefixes):
        return None, None, (jsonify(error="couldn't tell which provider that key is for - pick one "
                                    "in the Model provider dropdown"), 400)
    api_key = byo or next((os.environ[k] for k in _SERVER_KEY_ENVS if os.environ.get(k)), None)
    if not api_key:
        return None, None, (jsonify(error="this needs an API key - paste an Anthropic, OpenAI or "
                                    "Google key in the field, or set one on the server"), 400)
    return api_key, provider, None


@app.route("/run", methods=["POST"])
def run():
    body = request.get_json(force=True, silent=True) or {}
    mode = body.get("mode")
    target = TARGETS.get(body.get("target", "mock-core-banking"))
    if target is None:
        return jsonify(error="unknown target"), 400
    # fault injection - only on a target that supports it, and only a known kind
    inject = (body.get("inject") or "").strip()
    if inject and (not target["supports_inject"] or inject not in INJECT_KINDS):
        return jsonify(error="unknown or unsupported inject kind"), 400
    entry = target["entry"] + (f"?inject={inject}" if inject else "")
    try:
        if mode == "replay":
            cap_path = body.get("capability", "")
            if not cap_path or not Path(runner.REPO / cap_path).is_file():
                return jsonify(error="pick a capability"), 400
            params = body.get("params") or {}
            overrides = body.get("overrides") or {}
            r = runner.start("replay",
                             capability_path=str(runner.REPO / cap_path),
                             params={k: str(v) for k, v in params.items() if v not in (None, "")},
                             overrides={k: str(v) for k, v in overrides.items() if v not in (None, "")},
                             confirm=bool(body.get("confirm")), inject=inject)
        elif mode == "discovery":
            goal = (body.get("goal") or "").strip()
            if not goal:
                return jsonify(error="a goal is required for discovery"), 400
            api_key, provider, err = _resolve_model_key(body)
            if err:
                return err
            r = runner.start("discovery",
                             goal=goal, target_url=entry,
                             capability_id=(body.get("capability_id") or "discovered").strip(),
                             app_name=target["app_name"], api_key=api_key, provider=provider)
        elif mode == "ask":
            req = (body.get("request") or "").strip()
            if not req:
                return jsonify(error="type what you want done"), 400
            api_key, provider, err = _resolve_model_key(body)
            if err:
                return err
            r = runner.start("ask", request=req, api_key=api_key, provider=provider,
                             target_url=entry, app_name=target["app_name"])
        else:
            return jsonify(error="mode must be 'replay', 'discovery' or 'ask'"), 400
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(run_id=r.id)


@app.route("/runs")
def runs():
    """History view: the run registry (agent_interface/runs.py), newest first. `?id=` returns
    one run's full record."""
    rid = request.args.get("id")
    if rid:
        row = get_run(rid)
        return (jsonify(row) if row else (jsonify(error="no such run"), 404))
    return jsonify(runs=list_runs(limit=int(request.args.get("limit", "60"))))


@app.route("/stream")
def stream():
    r = runner.current()
    if r is None:
        return Response("event: done\ndata: {}\n\n", mimetype="text/event-stream")

    def gen():
        yield "retry: 2000\n\n"
        while True:
            try:
                ev = r.events.get(timeout=20)
            except Exception:
                yield ": keepalive\n\n"
                continue
            yield "data: " + json.dumps(ev) + "\n\n"
            if ev.get("type") == "done":
                break

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@app.route("/resume", methods=["POST"])
def resume():
    body = request.get_json(force=True, silent=True) or {}
    decision = body.get("decision")
    if decision not in ("approved", "declined", None):
        return jsonify(error="decision must be approved / declined"), 400
    controller.signal_resume(body.get("note", "via web console"), decision)
    return jsonify(ok=True, lease=controller.read_lease().state)


@app.route("/stop", methods=["POST"])
def stop():
    r = runner.current()
    if r is not None:
        r.stop()
    return jsonify(ok=True)


def main():
    if _GENERATED:
        print("\n" + "=" * 68)
        print("  live console - no CONSOLE_ACCESS_KEY set, generated one for this run:")
        print(f"    http://localhost:5055/?key={SECRET}")
        print("  share that link; anyone with it can drive a run. Set CONSOLE_ACCESS_KEY")
        print("  to a stable value to keep the link constant across restarts.")
        print("=" * 68 + "\n")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5055")), threaded=True)


if __name__ == "__main__":
    main()
