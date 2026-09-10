"""
Second target for the live console: a deliberately HOSTILE-DOM version of a member-servicing
app, plus per-request fault injection (the MERIDIAN-style `?inject=` switch).

Hostile markup, on purpose:
  - no `<label for=...>` anywhere - fields carry only `aria-label` (so a name still exists,
    but the classic label-association is gone)
  - clickable `<div role="button" tabindex="0">`, never a real `<button>`
  - results are a grid of nested `<div>`s, not a `<table>`
  - values buried in nested `<span>`s; the balance's accessible name comes via
    `aria-labelledby`, not structure
  - class-name soup (`f f-q`, `b b-go`, `tr td`), no test ids, no landmarks
ARIA *roles* are kept, so the perception layer's role+name tier still resolves - this stresses
messy markup and the label-proximity fallbacks without being unlocatable.

Fault injection - append `?inject=<kind>` to any URL (links + form actions carry it forward).
Each maps to a distinct, real replay-engine outcome:
  - `maintenance` : every request 503s with a declared "Service temporarily unavailable" page
                    -> the capability's `recoverable` expected_outcome fires -> `recoverable_handled`
  - `slow`        : ~4s delay per request, then a normal response -> completes under latency
                    (the hostile capabilities carry a wider `timeout_ms` so this still passes)
  - `blank`       : 200 and structurally intact, but the balance value renders empty
                    -> the extract contract fails -> `data_unavailable` (page-only access, datum gone)

Run:  python app2/app.py           # http://localhost:5051/find
"""
from __future__ import annotations

import os
import time

from flask import Flask, Response, redirect, request

app = Flask(__name__)

MEMBERS = {
    "12345": {"name": "Jordan Lee", "status": "active", "balance": "$1,842.30"},
    "99999": {"name": "Priya Shah", "status": "locked", "balance": "$0.00"},
    "77777": {"name": "Sam Rivera", "status": "active", "balance": ""},  # balance field never resolves
}
_INJECT_KINDS = {"", "maintenance", "slow", "blank"}


def _inject() -> str:
    k = request.args.get("inject", "")
    return k if k in _INJECT_KINDS else ""


def _q(path: str) -> str:
    """Carry the current inject kind onto an in-app link/form action."""
    k = _inject()
    return f"{path}?inject={k}" if k else path


_MAINT_HTML = """<!doctype html><meta charset=utf-8><title>unavailable</title>
<div class="wrap"><div class="pnl">
  <div role="alert" class="msg err">Service temporarily unavailable - please retry.</div>
  <div class="sub">The servicing console is in a short maintenance window.</div>
</div></div>"""


@app.before_request
def _fault_gate():
    if request.path == "/health":
        return None
    kind = _inject()
    if kind == "maintenance":
        return Response(_MAINT_HTML, status=503, headers={"Retry-After": "2"},
                        mimetype="text/html")
    if kind == "slow":
        time.sleep(4)
    return None


_STYLE = """<style>
  body{font:14px/1.5 -apple-system,Segoe UI,sans-serif;background:#eef1f4;color:#1e262b;margin:0}
  .wrap{max-width:620px;margin:28px auto;padding:0 16px}
  .pnl{background:#fff;border:1px solid #d3dae0;border-radius:6px;padding:18px 20px;margin-bottom:14px}
  .cap{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#8a969e;margin-bottom:8px}
  .f{width:100%;padding:8px 9px;border:1px solid #b7c1c8;border-radius:4px;font:inherit;margin:4px 0 10px}
  .b{display:inline-block;padding:8px 15px;border-radius:4px;background:#1f5673;color:#fff;
     font-weight:600;cursor:pointer;user-select:none;margin-top:4px}
  .b-warn{background:#9a3b28}
  .tbl{margin-top:6px} .tr{display:flex;gap:10px;padding:7px 0;border-bottom:1px solid #e4e9ec}
  .tr.h{color:#8a969e;font-size:12px} .td{flex:1} .td.a{flex:0 0 70px}
  .lk{color:#1f5673;font-weight:600}
  .kv{display:flex;justify-content:space-between;padding:6px 0}
  .kv .k{color:#7b8791} .kv .v{font-weight:700}
  .msg{padding:8px 10px;border-radius:4px;background:#f2f4f6}
  .msg.err{background:#fbe9e6;color:#9a3b28} .msg.ok{background:#e7f3ec;color:#1c6b3f}
  .sub{color:#7b8791;font-size:12.5px;margin-top:6px}
</style>"""


def _page(body: str, status: int = 200) -> Response:
    return Response(
        f"<!doctype html><meta charset=utf-8><title>servicing</title>{_STYLE}"
        f'<div class="wrap">{body}</div>',
        status=status, mimetype="text/html",
    )


@app.route("/")
def home():
    return redirect(_q("/find"))


@app.route("/find", methods=["GET", "POST"])
def find():
    rows = None
    q = ""
    if request.method == "POST":
        q = (request.form.get("q") or "").strip()
        if q:
            rows = [(mid, m) for mid, m in MEMBERS.items()
                    if q == mid or q.lower() in m["name"].lower()]
    body = f"""
    <div class="pnl">
      <div class="cap">Member search</div>
      <form method="post" action="{_q('/find')}">
        <div class="lbl">Search</div>
        <input class="f f-q" name="q" value="{q}" aria-label="Member ID or name"
               placeholder="member id / name">
        <div class="b b-go" role="button" tabindex="0"
             onclick="this.closest('form').submit()">Go</div>
      </form>
    </div>"""
    if rows is not None:
        if rows:
            trs = "".join(
                f'<div class="tr"><div class="td">{mid}</div>'
                f'<div class="td">{m["name"]}</div>'
                f'<div class="td a"><a class="lk" role="link" href="{_q("/acct/" + mid)}">open</a></div></div>'
                for mid, m in rows)
            body += f'<div class="pnl"><div class="cap">Results</div><div class="tbl">{trs}</div></div>'
        else:
            body += '<div class="pnl"><div class="msg">No matching member.</div></div>'
    return _page(body)


@app.route("/acct/<mid>")
def acct(mid: str):
    m = MEMBERS.get(mid)
    if m is None:
        return _page('<div class="pnl"><div class="msg err">No member record found.</div></div>', 404)
    if m["status"] == "locked":
        return _page('<div class="pnl"><div class="msg err">Access denied. '
                     'This account is restricted.</div></div>')
    bal = "" if _inject() == "blank" else m["balance"]
    body = f"""
    <div class="pnl">
      <div class="cap">Account - {m['name']}</div>
      <div class="kv">
        <span class="k" id="bk-{mid}">Current balance</span>
        <span class="v" role="status" aria-labelledby="bk-{mid}">{bal}</span>
      </div>
      <a class="b" role="button" href="{_q('/acct/' + mid + '/move')}">Move funds</a>
    </div>"""
    return _page(body)


@app.route("/acct/<mid>/move", methods=["GET", "POST"])
def move(mid: str):
    m = MEMBERS.get(mid)
    if m is None:
        return _page('<div class="pnl"><div class="msg err">No member record found.</div></div>', 404)
    if m["status"] == "locked":
        return _page('<div class="pnl"><div class="msg err">Access denied. '
                     'This account is restricted.</div></div>')
    stage = request.form.get("stage", "")
    amount = (request.form.get("amount") or "").strip()
    dest = (request.form.get("dest") or "").strip()

    if request.method == "POST" and stage == "confirm":
        ref = f"TR-{1000 + (abs(hash(mid + amount + dest)) % 9000)}"
        return _page(f'<div class="pnl"><div class="cap">Account - {m["name"]}</div>'
                     f'<div class="kv"><span class="k" id="rk">Transfer reference</span>'
                     f'<span class="v" role="status" aria-labelledby="rk">{ref}</span></div>'
                     f'<div class="msg ok">Transfer complete.</div></div>')

    review = ""
    if request.method == "POST" and stage == "review" and amount and dest:
        review = f"""
        <div class="pnl">
          <div class="cap">Review</div>
          <div class="ln">Move {amount} to {dest}</div>
          <form method="post" action="{_q('/acct/' + mid + '/move')}">
            <input type="hidden" name="stage" value="confirm">
            <input type="hidden" name="amount" value="{amount}">
            <input type="hidden" name="dest" value="{dest}">
            <div class="b b-warn" role="button" tabindex="0"
                 onclick="this.closest('form').submit()">Confirm transfer</div>
          </form>
        </div>"""
    body = f"""
    <div class="pnl">
      <div class="cap">Move funds - {m['name']}</div>
      <form method="post" action="{_q('/acct/' + mid + '/move')}">
        <input type="hidden" name="stage" value="review">
        <div class="lbl">Amount</div>
        <input class="f" name="amount" value="{amount}" aria-label="Amount to move" placeholder="$0.00">
        <div class="lbl">Destination</div>
        <input class="f" name="dest" value="{dest}" aria-label="Destination account" placeholder="acct #">
        <div class="b" role="button" tabindex="0"
             onclick="this.closest('form').submit()">Review transfer</div>
      </form>
    </div>{review}"""
    return _page(body)


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    # HOSTILE_PORT (not PORT) so it never collides with the console's own PORT under serve.sh
    app.run(host="0.0.0.0", port=int(os.environ.get("HOSTILE_PORT", "5051")), threaded=True)
