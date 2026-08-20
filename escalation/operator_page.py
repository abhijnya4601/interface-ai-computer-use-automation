"""
Minimal, deliberately bare/ugly operator console — the assignment's scope note explicitly
allows this UI to be minimal as long as the mechanism underneath (the lease flip, the
non-headless persistent Playwright session) is real. Shows the current escalation context and
screenshot, and a Resume button that writes the resume signal controller.py is polling for.

Requires HTTP Basic Auth: whoever can reach this page can approve an irreversible
financial action, so "no auth at all" is a genuine safety gap, not a cosmetic one — the
assignment's "operator UI can be bare" allowance is about polish, not about access control.
Credentials come from OPERATOR_USERNAME / OPERATOR_PASSWORD env vars; if OPERATOR_PASSWORD isn't
set, a random one is generated and printed to the console for this run only (fail-secure: never
silently serve unauthenticated, but also never hard-fail a fresh checkout with no setup step).

Run on a separate port from the mock bank app (5001) so both can run at once:
    python escalation/operator_page.py
"""
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, Response, redirect, render_template_string, request, send_file, url_for

from escalation.controller import read_lease, signal_resume

app = Flask(__name__)

OPERATOR_USERNAME = os.environ.get("OPERATOR_USERNAME", "banker")
OPERATOR_PASSWORD = os.environ.get("OPERATOR_PASSWORD")
if not OPERATOR_PASSWORD:
    OPERATOR_PASSWORD = secrets.token_urlsafe(16)
    print("[operator_page] OPERATOR_PASSWORD not set — generated a one-time credential for this run:")
    print(f"[operator_page]   username: {OPERATOR_USERNAME}")
    print(f"[operator_page]   password: {OPERATOR_PASSWORD}")
    print("[operator_page] Set OPERATOR_USERNAME/OPERATOR_PASSWORD env vars for a stable credential.")


def _check_auth(username: str | None, password: str | None) -> bool:
    return bool(
        username and password
        and secrets.compare_digest(username, OPERATOR_USERNAME)
        and secrets.compare_digest(password, OPERATOR_PASSWORD)
    )


@app.before_request
def _require_auth():
    auth = request.authorization
    if not auth or not _check_auth(auth.username, auth.password):
        return Response(
            "Authentication required to access the operator console.\n",
            401,
            {"WWW-Authenticate": 'Basic realm="Operator Console"'},
        )


@app.after_request
def _no_cache(response):
    # This page shows live escalation state (D23-adjacent finding: a browser back-button or
    # reload can redisplay an already-resolved "escalated" view from cache, making a resolved
    # request look like it's still pending -- confusing for an operator deciding whether to act).
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Operator Console</title></head>
<body>
<h1>Operator Console</h1>
{% if resumed %}
  <p style="background: #dfd; border: 1px solid #6a6; padding: 8px 12px;">
    Resume signal sent (decision: {{ resumed }}). The waiting discovery/replay process will pick
    it up within a second or two and continue on its own.</p>
{% endif %}
{% if lease.state == 'human' %}
  <p><b>Status:</b> escalated — waiting for a human operator</p>
  <p><b>Reason:</b> {{ lease.context.get('reason') }}</p>
  <p><b>Current URL:</b> {{ lease.context.get('current_url') }}</p>
  <p><b>Run ID:</b> {{ lease.context.get('run_id') }}</p>
  {% if lease.context.get('screenshot_path') %}
    <p><img src="/screenshot" style="max-width: 900px; border: 1px solid #999"></p>
  {% endif %}
  <p>Take over the live browser window now (it is still open — this is the same session the
     automation was driving, not a new one) if you need to. Then record what you decided and
     resume: Approve if the agent should go ahead with the risky action it paused on, Decline if
     it should not, or plain Resume if this was a stuck/dead-end recovery where approve/decline
     doesn't apply (you fixed something manually and it should just carry on).</p>
  <form method="POST" action="/resume">
    <label for="summary">What did you do / decide? (recorded as evidence)</label><br>
    <textarea id="summary" name="summary" rows="3" cols="60"></textarea><br>
    <button type="submit" name="decision" value="approved">Approve &amp; Resume</button>
    <button type="submit" name="decision" value="declined">Decline &amp; Resume</button>
    <button type="submit" name="decision" value="">Resume (no decision needed)</button>
  </form>
{% else %}
  <p>No escalation is currently active. Automation is in control.</p>
{% endif %}
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(TEMPLATE, lease=read_lease(), resumed=request.args.get("resumed"))


@app.route("/screenshot")
def screenshot():
    lease = read_lease()
    path = lease.context.get("screenshot_path")
    if not path or not Path(path).exists():
        return "no screenshot available", 404
    return send_file(path)


@app.route("/resume", methods=["POST"])
def resume_route():
    decision = request.form.get("decision") or None
    signal_resume(
        human_actions_summary=request.form.get("summary", ""),
        decision=decision,
    )
    return redirect(url_for("index", resumed=decision or "no-decision-needed"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
