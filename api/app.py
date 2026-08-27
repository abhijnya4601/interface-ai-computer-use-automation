"""
Capability API + dashboard — one Flask app (reuse the stack; the mock app and operator console
are already Flask). Single process, synchronous, one global browser lock. A queue / DB /
websocket dashboard would be the scaling infrastructure the brief says not to build.

JSON API
    GET  /api/capabilities                     -> the callable catalog (typed args, risk, role)
    POST /api/capabilities/<id>/invoke         -> {args, role?} -> Result + run_id (no `confirm`)
    GET  /api/runs                             -> run history (discovery + replay), newest first
    GET  /api/runs/<run_id>                    -> one run
    GET  /api/runs/<run_id>/evidence/<name>    -> an evidence file
    GET  /api/health

Dashboard (server-rendered, meta-refresh — no JS framework)
    GET  /                                     -> catalog + run history
    GET  /runs/<run_id>                        -> run detail + evidence

A risky capability is NOT gated by a `confirm` body field (an LLM could set it). It runs up to
the final click, then routes an intervention request to the operator console
(escalation/operator_page.py, started by this app on :5001) and only commits on human approval.

Run:  python -m api            (or python api/app.py)  -> http://localhost:8000
Needs MERIDIAN_OPERATOR / MERIDIAN_PASSWORD in the environment for MERIDIAN capabilities.
"""
from __future__ import annotations

import html
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, abort, jsonify, redirect, request, send_file, url_for

from agent_interface.catalog import _NOT_A_TOOL, build_tool_catalog, load_capabilities
from agent_interface.invoke import invoke_capability
from agent_interface.runs import get_run, list_runs, record_run
from agent.session import run_with_session
from artifact.schema import Capability

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"
OPERATOR_BASE = "http://localhost:5001"
SESSION_REQUIRED_HOSTS = {"web-sample.interface-hiring.com"}
ESCALATION_MAX_WAIT_S = 600.0  # a human may take a while at the operator console

app = Flask(__name__)
_invoke_lock = threading.Lock()  # replay drives one browser; concurrent invokes queue
_operator_proc: subprocess.Popen | None = None


# ---- operator console lifecycle -------------------------------------------------------------

def _ensure_operator_console() -> None:
    global _operator_proc
    if _operator_proc and _operator_proc.poll() is None:
        return
    env = dict(os.environ)
    env.setdefault("OPERATOR_USERNAME", "banker")
    env.setdefault("OPERATOR_PASSWORD", secrets.token_urlsafe(12))
    _operator_proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent.parent / "escalation" / "operator_page.py")],
        env=env,
    )
    time.sleep(1.5)
    print(f"[api] operator console -> {OPERATOR_BASE}  "
          f"(user {env['OPERATOR_USERNAME']} / pass {env['OPERATOR_PASSWORD']})")


# ---- helpers ------------------------------------------------------------------------------------

def _needs_session(cap: Capability) -> bool:
    return urlparse(cap.target.entry_point).netloc in SESSION_REQUIRED_HOSTS


def _evidence_refs_for(run_id: str) -> list[str]:
    refs = []
    for p in sorted(EVIDENCE_DIR.glob(f"*{run_id}*")):
        refs.append(p.name)
    return refs


def _do_invoke(cap: Capability, args: dict, role: str | None) -> tuple[dict, int]:
    run_id = f"api_{int(time.time() * 1000)}"
    started = time.time()
    with _invoke_lock:
        if _needs_session(cap):
            _ensure_operator_console()
            result = run_with_session(
                cap, args, role=role, run_id=run_id,
                risky_mode="escalate", escalation_max_wait_s=ESCALATION_MAX_WAIT_S,
            )
        elif cap.risk_level == "risky":
            _ensure_operator_console()
            from replay.engine import replay
            result = replay(cap, args, run_id=run_id, risky_mode="escalate",
                            escalation_max_wait_s=ESCALATION_MAX_WAIT_S)
        else:
            result = invoke_capability(cap.capability_id, args)

    entry = record_run(
        run_id, "replay", cap.capability_id,
        status=result.status, params=args, outputs=result.outputs,
        business_outcome_code=result.business_outcome_code,
        failure_detail=result.failure_detail, started_at=started, recovery=result.recovery,
        evidence_refs=_evidence_refs_for(run_id),
        extra={"via": "api"},
    )
    http_code = 200 if result.status in ("success", "business_outcome", "recoverable_handled") else 409
    return {"run_id": run_id, "result": result.model_dump(), "run": entry}, http_code


# ---- JSON API ---------------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return jsonify(ok=True, capabilities=len(load_capabilities()))


@app.get("/api/capabilities")
def capabilities():
    caps = load_capabilities()
    catalog = build_tool_catalog(include_preconditions=True)
    for tool in catalog:
        cap = caps.get(tool["name"])
        if cap:
            tool["risk_level"] = cap.risk_level
            tool["requires_role"] = cap.requires_role
            tool["target"] = cap.target.entry_point
            tool["needs_session"] = _needs_session(cap)
    return jsonify(catalog)


@app.post("/api/capabilities/<cap_id>/invoke")
def invoke(cap_id: str):
    caps = load_capabilities()
    if cap_id not in caps:
        abort(404, f"unknown capability {cap_id!r}")
    if cap_id in _NOT_A_TOOL:
        abort(400, f"{cap_id!r} is a session precondition, not directly invocable")
    body = request.get_json(silent=True) or {}
    args = body.get("args", body.get("params", {}))
    role = body.get("role")  # NOT `confirm` — deliberately not accepted from the caller
    payload, code = _do_invoke(caps[cap_id], args, role)
    return jsonify(payload), code


@app.get("/api/runs")
def runs():
    return jsonify(list_runs(limit=request.args.get("limit", type=int)))


@app.get("/api/runs/<run_id>")
def run_detail(run_id: str):
    row = get_run(run_id)
    if not row:
        abort(404)
    return jsonify(row)


@app.get("/api/runs/<run_id>/evidence/<path:name>")
def run_evidence(run_id: str, name: str):
    if "/" in name or ".." in name:
        abort(400)
    path = EVIDENCE_DIR / name
    if not path.exists():
        abort(404)
    return send_file(path)


# ---- dashboard (server-rendered) -----------------------------------------------------------

_STYLE = """
<style>
 body{font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f5f7;color:#1a1a1a}
 header{background:#003366;color:#fff;padding:10px 18px;font-weight:600}
 main{padding:18px;max-width:1100px;margin:0 auto}
 h2{font-size:14px;margin:22px 0 8px;color:#333}
 table{border-collapse:collapse;width:100%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.08)}
 th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #e3e6ea;vertical-align:top}
 th{background:#eef2f7;font-size:11px;text-transform:uppercase;letter-spacing:.03em;color:#556}
 .s{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;font-weight:600}
 .success{background:#d7f0dd;color:#1c6b32}.business_outcome{background:#dce8fb;color:#1b4b8a}
 .recoverable_handled{background:#fdf0d2;color:#8a5b12}.hard_failure{background:#f7d7d7;color:#9a2323}
 .escalated{background:#efe0f7;color:#6b2b8a}
 code{background:#f0f1f3;padding:1px 4px;border-radius:3px;font-size:12px}
 a{color:#0b5bd3;text-decoration:none}a:hover{text-decoration:underline}
 .muted{color:#888}
 img{max-width:900px;border:1px solid #ccc;margin-top:6px}
</style>
"""


def _status_badge(s: str) -> str:
    return f'<span class="s {html.escape(s or "")}">{html.escape(s or "?")}</span>'


@app.get("/")
def dashboard():
    caps = load_capabilities()
    cap_rows = ""
    for tool in build_tool_catalog(include_preconditions=True):
        cap = caps.get(tool["name"])
        risk = cap.risk_level if cap else "?"
        role = (cap.requires_role if cap else None) or "teller"
        tag = "" if tool.get("invocable", True) else " <span class='muted'>(precondition)</span>"
        cap_rows += (
            f"<tr><td><code>{html.escape(tool['name'])}</code>{tag}</td>"
            f"<td>{html.escape(tool['description'])}</td>"
            f"<td>{html.escape(risk)}</td><td>{html.escape(role)}</td>"
            f"<td class='muted'>{html.escape(', '.join(tool['input_schema']['properties']))}</td></tr>"
        )

    run_rows = ""
    for r in list_runs(limit=50):
        rid = html.escape(r.get("run_id", ""))
        run_rows += (
            f"<tr><td><a href='{url_for('run_page', run_id=rid)}'>{rid}</a></td>"
            f"<td>{html.escape(r.get('kind',''))}</td>"
            f"<td>{html.escape(r.get('capability_id',''))}</td>"
            f"<td>{_status_badge(r.get('status'))}"
            f"{(' <span class=muted>'+html.escape(str(r.get('business_outcome_code')))+'</span>') if r.get('business_outcome_code') else ''}</td>"
            f"<td class='muted'>{html.escape(str(r.get('params') or {}))}</td>"
            f"<td class='muted'>{html.escape(str(r.get('outputs') or {}))}</td>"
            f"<td class='muted'>{r.get('duration_s','')}s</td>"
            f"<td class='muted'>{html.escape(time.strftime('%H:%M:%S', time.localtime(r.get('finished_at', 0))))}</td></tr>"
        )

    return f"""<!doctype html><meta charset=utf-8><title>Capability dashboard</title>
<meta http-equiv=refresh content=4>{_STYLE}
<header>Computer-Use Capability Dashboard <span style='font-weight:400;opacity:.8'>— MERIDIAN CORE adaptation</span></header>
<main>
<h2>Capabilities ({len(caps)})</h2>
<table><tr><th>id</th><th>what it does</th><th>risk</th><th>role</th><th>inputs</th></tr>{cap_rows}</table>
<h2>Runs — discovery &amp; replay, newest first (auto-refresh 4s)</h2>
<table><tr><th>run</th><th>kind</th><th>capability</th><th>status</th><th>params</th><th>outputs</th><th>dur</th><th>at</th></tr>{run_rows or '<tr><td colspan=8 class=muted>no runs yet</td></tr>'}</table>
</main>"""


@app.get("/runs/<run_id>")
def run_page(run_id: str):
    r = get_run(run_id)
    if not r:
        abort(404)
    evid = ""
    for name in r.get("evidence_refs", []):
        url = url_for("run_evidence", run_id=run_id, name=name)
        if name.lower().endswith(".png"):
            evid += f"<div><a href='{url}'>{html.escape(name)}</a><br><img src='{url}'></div>"
        else:
            evid += f"<div><a href='{url}'>{html.escape(name)}</a></div>"
    fields = ""
    for k in ("kind", "status", "business_outcome_code", "params", "outputs",
              "failure_detail", "duration_s", "tier_log"):
        fields += f"<tr><th>{k}</th><td><code>{html.escape(str(r.get(k)))}</code></td></tr>"
    return f"""<!doctype html><meta charset=utf-8><title>run {html.escape(run_id)}</title>{_STYLE}
<header><a style='color:#cfe0f5' href='/'>&larr; dashboard</a> &nbsp; run {html.escape(run_id)}</header>
<main>
<h2>{html.escape(r.get('capability_id',''))} &nbsp; {_status_badge(r.get('status'))}</h2>
<table>{fields}</table>
<h2>Evidence</h2>{evid or "<p class=muted>none</p>"}
</main>"""


@app.post("/resume")  # convenience passthrough so a demo link can point at the API host
def resume_passthrough():
    return redirect(OPERATOR_BASE)


if __name__ == "__main__":
    print("[api] capability API + dashboard on http://localhost:8000")
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
