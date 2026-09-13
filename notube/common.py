"""Shared infrastructure: force IPv4, the run-artifacts dir, a retry helper, Chromium flags.

Importing this module forces every socket to use IPv4 and sets a 30s default
timeout. In this environment IPv6 egress to Google hangs ~80s before falling
back, so we filter getaddrinfo to IPv4 only. Network modules import this first.
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

socket.setdefaulttimeout(30)
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host, *args, **kwargs):
    return [r for r in _orig_getaddrinfo(host, *args, **kwargs) if r[0] == socket.AF_INET]


socket.getaddrinfo = _ipv4_only

# Everything a run produces (logs, progress/state files, downloaded subs) goes under
# this one gitignored dir so the repo root stays source-only.
RUN_DIR = Path(__file__).resolve().parent.parent / "run"  # the project root, not the package


def run_path(*parts: str) -> Path:
    """A path under RUN_DIR, with its parent directories created."""
    p = RUN_DIR.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def retry(fn, *, tries: int = 5, base: float = 1.0, cap: float = 30.0,
          on: tuple = (Exception,), unless=None):
    """Call fn() with exponential backoff.

    Retries when fn raises one of the exception types in `on`, sleeping
    base, 2*base, ... up to `cap` seconds between attempts. If `unless(exc)`
    returns True the error is treated as non-transient and re-raised at once.
    The original exception is re-raised after the last attempt.
    """
    delay = base
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except on as exc:
            if attempt == tries or (unless is not None and unless(exc)):
                raise
            time.sleep(delay)
            delay = min(delay * 2, cap)


def chromium_launch_args() -> list[str]:
    """Chromium flags shared by every headless browser path in this repo.

    Chromium refuses to start as root with its sandbox on ("Running as root without
    --no-sandbox is not supported") — and root is exactly how the daily cron runs
    inside its container. Drop the sandbox only there; a normal (macOS) run keeps it.
    The same branch routes renderer shared memory to /tmp, the standard container
    mitigation for the intermittent renderer crashes seen under load. Belt-and-braces,
    not a proven cure — the callers retry as well.
    """
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args += ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
    return args
