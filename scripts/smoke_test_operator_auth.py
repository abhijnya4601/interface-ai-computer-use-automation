"""
Live integration smoke test for the operator console authentication — launches the real
escalation/operator_page.py as a real subprocess (exactly like run_discovery.py's
--auto-approve-escalation does) and drives it over real HTTP, proving:
  1. an unauthenticated request is rejected (401), including /resume specifically
  2. a correctly-authenticated request succeeds
  3. an unauthenticated /resume attempt does NOT flip the lease — the single most
     safety-critical property this whole feature exists for

No browser, no Anthropic API call needed — this only exercises the operator console + lease
mechanism, using escalation/controller.py directly to simulate an active escalation.

Run: python scripts/smoke_test_operator_auth.py
"""
import base64
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from escalation import controller

OPERATOR_BASE = "http://localhost:5001"


def _basic_auth_header(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def check(label: str, condition: bool):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"Smoke test failed: {label}")


class FakePage:
    url = "http://localhost:5050/member/12345/new-subaccount"

    def screenshot(self, path):
        with open(path, "wb") as f:
            f.write(b"fake-png-bytes-for-smoke-test")


def main():
    if controller.LEASE_PATH.exists():
        controller.LEASE_PATH.unlink()
    if controller.RESUME_SIGNAL_PATH.exists():
        controller.RESUME_SIGNAL_PATH.unlink()

    username = "smoke-test-operator"
    password = secrets.token_urlsafe(16)
    auth_headers = _basic_auth_header(username, password)

    operator_env = {**os.environ, "OPERATOR_USERNAME": username, "OPERATOR_PASSWORD": password}
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent.parent / "escalation" / "operator_page.py")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=operator_env,
    )
    time.sleep(1.5)

    try:
        # 1. unauthenticated GET is rejected
        try:
            urllib.request.urlopen(f"{OPERATOR_BASE}/")
            got_401 = False
        except urllib.error.HTTPError as e:
            got_401 = e.code == 401
        check("unauthenticated GET / is rejected with 401", got_401)

        # 2. correctly-authenticated GET succeeds
        req = urllib.request.Request(f"{OPERATOR_BASE}/", headers=auth_headers)
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode()
        check("authenticated GET / succeeds with 200", "Operator Console" in body)

        # 3. wrong password is rejected
        wrong_headers = _basic_auth_header(username, "totally-wrong-password")
        try:
            urllib.request.urlopen(urllib.request.Request(f"{OPERATOR_BASE}/", headers=wrong_headers))
            wrong_pw_rejected = False
        except urllib.error.HTTPError as e:
            wrong_pw_rejected = e.code == 401
        check("wrong password is rejected with 401", wrong_pw_rejected)

        # Simulate a real active escalation directly via the controller (no browser needed).
        import threading
        escalation_thread = threading.Thread(
            target=lambda: controller.trigger_escalation(
                "smoke test escalation", FakePage(), run_id="smoke_auth_test", poll_interval_s=0.1,
            )
        )
        escalation_thread.start()
        time.sleep(0.5)
        check("lease is human (escalation is active)", controller.read_lease().state == "human")

        # 4. THE critical case: unauthenticated /resume must NOT be able to approve anything
        resume_data = "decision=approved&summary=unauthenticated+attempt".encode()
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"{OPERATOR_BASE}/resume", data=resume_data, method="POST")
            )
            unauth_resume_blocked = False
        except urllib.error.HTTPError as e:
            unauth_resume_blocked = e.code == 401
        check("unauthenticated POST /resume is rejected with 401", unauth_resume_blocked)
        check("lease is STILL human after the rejected unauthenticated resume attempt",
              controller.read_lease().state == "human")

        # 5. correctly-authenticated /resume succeeds and actually flips the lease
        auth_resume_data = "decision=approved&summary=authenticated+approval".encode()
        req = urllib.request.Request(
            f"{OPERATOR_BASE}/resume", data=auth_resume_data, method="POST", headers=auth_headers
        )
        urllib.request.urlopen(req)
        escalation_thread.join(timeout=5)
        check("lease flipped back to automation after authenticated resume",
              controller.read_lease().state == "automation")

    finally:
        proc.terminate()

    print("\nAll operator console auth smoke checks passed.")


if __name__ == "__main__":
    main()
