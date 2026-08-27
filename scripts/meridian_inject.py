"""
Out-of-band fault-injection control for MERIDIAN CORE's System Settings screen — test/demo
infrastructure, NOT something any capability does (the automation's allowlist blocks /settings
in both phases, on purpose: the wrapper must never be able to disable fault handling).

    python scripts/meridian_inject.py set maintenance   # force every posting action -> 503
    python scripts/meridian_inject.py set ""            # clear
    python scripts/meridian_inject.py clear
    python scripts/meridian_inject.py rate 0.5          # 50% random error rate on posts

Kinds: validation notfound permission timeout maintenance server  (brief §2.2)
Needs MERIDIAN_OPERATOR / MERIDIAN_PASSWORD in the environment.
"""
from __future__ import annotations

import os
import re
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar

BASE = "https://web-sample.interface-hiring.com"
KINDS = {"", "validation", "notfound", "permission", "timeout", "maintenance", "server"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # the login cookie is set on the 302 itself; don't chase /menu (it may 503)


def _client():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar), _NoRedirect()
    )


# A non-empty, non-kind ?inject= value overrides a stuck global forcedInject for that one
# request — this is how this tool always reaches /settings even when every authenticated route
# is 503ing under a forced maintenance inject.
def _u(path):
    return f"{BASE}{path}{'&' if '?' in path else '?'}inject=none"


def _post(opener, path, data):
    body = urllib.parse.urlencode(data).encode()
    try:
        return opener.open(urllib.request.Request(_u(path), data=body), timeout=20)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303):
            return exc  # a redirect after a successful POST — fine
        raise


def _signon(opener):
    op = os.environ.get("MERIDIAN_OPERATOR")
    pw = os.environ.get("MERIDIAN_PASSWORD")
    br = os.environ.get("MERIDIAN_BRANCH", "MAIN-001")
    if not op or not pw:
        sys.exit("set MERIDIAN_OPERATOR / MERIDIAN_PASSWORD")
    try:
        _post(opener, "/signon", {"operator": op, "password": pw, "branch": br})
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303):
            raise  # 302 -> success (cookie set on the redirect response); anything else is real


def _token(opener):
    html = opener.open(_u("/settings"), timeout=20).read().decode("latin-1")
    m = re.search(r'name="_token"\s+value="([^"]+)"', html)
    return m.group(1) if m else ""


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    opener = _client()
    _signon(opener)
    tok = _token(opener)

    if cmd == "clear":
        _post(opener, "/settings", {"_token": tok, "errorRate": "0", "forcedInject": ""})
        print("cleared: forcedInject='' errorRate=0")
    elif cmd == "set":
        kind = sys.argv[2] if len(sys.argv) > 2 else ""
        if kind not in KINDS:
            sys.exit(f"unknown kind {kind!r}; one of {sorted(KINDS)}")
        _post(opener, "/settings", {"_token": tok, "errorRate": "0", "forcedInject": kind})
        print(f"forcedInject={kind!r}")
    elif cmd == "rate":
        rate = sys.argv[2] if len(sys.argv) > 2 else "0"
        _post(opener, "/settings", {"_token": tok, "errorRate": rate, "forcedInject": ""})
        print(f"errorRate={rate}")
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
