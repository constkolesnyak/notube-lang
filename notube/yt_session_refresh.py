"""Keep the YouTube session in the notube Chromium profile alive.

``innertube.py`` reads YouTube cookies straight out of the profile's SQLite store
(``NOTUBE_CHROME_PROFILE``). Google rotates the session cookies
(``__Secure-1PSIDTS`` / ``__Secure-3PSIDTS``) every few hours: a browser that keeps
visiting youtube.com gets the fresh ones written back into its profile and stays
logged in for months, while a *snapshot* of those cookies (the legacy
``NOTUBE_COOKIES_FILE`` mode) is invalidated as soon as the browser it was copied
from rotates — that snapshot died in under a day on 2026-07-31.

So the profile has to keep browsing. This drives a headless Chromium to youtube.com
once per run, which is enough for Google to re-issue the rotating cookies into the
profile's SQLite DB; the graceful close flushes them.

The profile needs a real YouTube login once (open youtube.com in it and sign in).
After that this keeps it alive unattended.

    uv run python -m notube.yt_session_refresh   # warm; exit 0 if logged in, 2 if not
"""

from __future__ import annotations

import glob
import os
import sys

from notube.common import chromium_launch_args  # one place owns the Chromium flags

HOME_URL = "https://www.youtube.com/"
CHROMIUM = os.environ.get("NOTUBE_CHROMIUM_BIN", "/usr/bin/chromium")


def warm(timeout: float = 60.0, verbose: bool = False) -> bool:
    """Visit youtube.com in the profile so Google re-issues its rotating cookies.

    Returns True if the page came back logged in. Best-effort: a launch/navigation
    failure raises; a logged-out result returns False so the caller can warn instead
    of guessing.
    """
    from dotenv import load_dotenv

    load_dotenv()
    profile = os.environ.get("NOTUBE_CHROME_PROFILE")
    if not profile:
        raise RuntimeError("NOTUBE_CHROME_PROFILE not set (the Chromium profile daily.py reads).")

    # A crashed Chromium leaves a Singleton* lock that blocks fresh launches.
    for p in glob.glob(os.path.join(profile, "Singleton*")):
        try:
            os.unlink(p)
        except OSError:
            pass

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            profile,
            headless=True,
            executable_path=CHROMIUM,
            args=chromium_launch_args(),
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:  # noqa: BLE001 — networkidle is a nice-to-have
                pass
            # Same signal innertube uses on the same HTML.
            logged_in = '"LOGGED_IN":true' in page.content()
            if verbose:
                print(f"final url: {page.url}  logged_in: {logged_in}")
        finally:
            ctx.close()  # graceful close flushes the refreshed cookies to SQLite
    return logged_in


if __name__ == "__main__":
    ok = warm(verbose=True)
    print("logged_in:", ok)
    sys.exit(0 if ok else 2)
