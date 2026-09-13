"""Keep the NOTUBE_COOKIES_FILE YouTube session alive between daily runs.

The container is the sole owner of this session. Two freshness tracks:
- youtube.com rotates SIDCC/__Secure-*PSIDCC via Set-Cookie on the homepage GET;
- __Secure-*PSIDTS (the WRITE-auth freshness token) only rotates through
  accounts.*/RotateCookies — without that poke, reads keep working while youtubei
  writes start 403ing once PSIDTS ages out (~16h observed, 2026-08-02/03).
daily.py persists its own rotations; this runs every few hours so neither track
lapses, and doubles as an early alarm.

    uv run python -m notube.yt_keepalive
    # exit 0 ok/skipped, 2 jar dead, 3 PSIDTS rotation failed, 1 misconfig
"""

import fcntl
import os
import sys

import httpx

from notube.common import retry  # importing common also forces IPv4 + socket timeout
from notube.innertube import ORIGIN, UA, load_cookie_jar, rotate_session_tokens, save_cookie_jar


def main() -> int:
    path = os.environ.get("NOTUBE_COOKIES_FILE")
    if not path:
        print("NOTUBE_COOKIES_FILE not set — nothing to keep alive")
        return 1
    # Same lock daily.py takes: while a daily run is rotating the jar itself,
    # a concurrent keepalive would only race it for the file.
    lock = open("/tmp/notube-daily.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("skipped: daily.py is running (it rotates the jar itself)")
        return 0
    jar = load_cookie_jar(path)
    before = {c.name: c.value for c in jar if "youtube.com" in (c.domain or "")}
    cl = httpx.Client(headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                      cookies=jar, follow_redirects=True, timeout=30.0)
    home = retry(lambda: cl.get(ORIGIN + "/").raise_for_status().text, tries=3,
                 on=(httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError))
    if '"LOGGED_IN":true' not in home:
        # Do NOT save: a good-enough file must never be overwritten by a dead state.
        print("DEAD: jar no longer logged in — quit Chrome on the host that owns the "
              "session and re-export its cookies into NOTUBE_COOKIES_FILE")
        return 2
    hosts = rotate_session_tokens(cl)  # persists the jar itself on success
    save_cookie_jar(cl)
    after = {c.name: c.value for c in jar if "youtube.com" in (c.domain or "")}
    rotated = sorted(n for n in after if before.get(n) != after[n])
    if not hosts:
        print(f"ROTATE-FAILED: logged in, but RotateCookies got no 200 — youtubei "
              f"writes will start 403ing once __Secure-*PSIDTS ages out (~16h); "
              f"jar saved; rotated: {', '.join(rotated) or 'none'}")
        return 3
    print(f"ok: logged in; jar saved ({len(after)} yt cookies); "
          f"PSIDTS via {', '.join(hosts)}; rotated: {', '.join(rotated) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
