"""
Minimal, deliberately bare/ugly operator console — the assignment's scope note explicitly
allows this UI to be minimal as long as the mechanism underneath (the lease flip, the
non-headless persistent Playwright session) is real. Shows the current escalation context and
screenshot, and a Resume button that writes the resume signal controller.py is polling for.

Run on a separate port from the mock bank app (5001) so both can run at once:
    python escalation/operator_page.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, redirect, render_template_string, request, send_file, url_for

from escalation.controller import read_lease, signal_resume

app = Flask(__name__)

TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Operator Console</title></head>
<body>
<h1>Operator Console</h1>
{% if lease.state == 'human' %}
  <p><b>Status:</b> escalated — waiting for a human operator</p>
  <p><b>Reason:</b> {{ lease.context.get('reason') }}</p>
  <p><b>Current URL:</b> {{ lease.context.get('current_url') }}</p>
  <p><b>Run ID:</b> {{ lease.context.get('run_id') }}</p>
  {% if lease.context.get('screenshot_path') %}
    <p><img src="/screenshot" style="max-width: 900px; border: 1px solid #999"></p>
  {% endif %}
  <p>Take over the live browser window now (it is still open — this is the same session the
     automation was driving, not a new one), perform whatever manual step is needed, then click
     Resume below.</p>
  <form method="POST" action="/resume">
    <label for="summary">What did you do? (recorded as evidence)</label><br>
    <textarea id="summary" name="summary" rows="3" cols="60"></textarea><br>
    <button type="submit">Resume automation</button>
  </form>
{% else %}
  <p>No escalation is currently active. Automation is in control.</p>
{% endif %}
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(TEMPLATE, lease=read_lease())


@app.route("/screenshot")
def screenshot():
    lease = read_lease()
    path = lease.context.get("screenshot_path")
    if not path or not Path(path).exists():
        return "no screenshot available", 404
    return send_file(path)


@app.route("/resume", methods=["POST"])
def resume_route():
    signal_resume(human_actions_summary=request.form.get("summary", ""))
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
