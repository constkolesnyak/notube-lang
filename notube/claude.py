"""The `claude` CLI, the transport only: one invocation, its answer, and what it cost.

It is a transport, so the surface is deliberately small.

Running `claude -p` rather than the Messages API rides the existing Claude Code subscription:
no API key, no `anthropic` dependency. Three measured consequences shape every caller:

* Every invocation carries Claude Code's own ~24k-token harness prompt, irreducibly. The unit
  to amortise is the *invocation*, so a request wants to be dozens of items, not one.
* There is no server-side schema. Every contract is plain lines matched on an echoed id, and
  a reply that comes back short is re-asked rather than trusted.
* It is slow and its cost is erratic, so callers keep per-run budgets and write after every
  call rather than at the end.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

TIMEOUT = 1800
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Claude Code reads project settings, hooks and MCP servers by default. None of that belongs
# in a headless call: prompt weight at best, a surprise at worst.
BASE_ARGS = ("--output-format", "json", "--strict-mcp-config",
             "--settings", '{"disableAllHooks":true}')


def executable() -> str:
    """The `claude` binary: NOTUBE_CLAUDE_BIN if set, else whatever is on PATH."""
    path = os.environ.get("NOTUBE_CLAUDE_BIN") or shutil.which("claude")
    if not path:
        raise RuntimeError("the `claude` CLI isn't on PATH — set NOTUBE_CLAUDE_BIN in .env, or install "
                           "it; this runs through the Claude Code subscription rather than an API key.")
    return path


def ask(text: str, system: str, model: str = "opus", effort: str | None = "high",
        timeout: int = TIMEOUT, what: str = "request") -> tuple[str, dict, float]:
    """One `claude -p` call. Returns (reply text, usage dict, notional cost).

    `effort` is named rather than left to the default because an unnamed request comes back
    bimodally — either terse or deliberating — which turns every comparison into a measurement
    of which mode was drawn. An unknown value is only *warned* about by the CLI and then
    ignored, so the levels are checked here.
    """
    if effort is not None and effort not in EFFORTS:
        raise ValueError(f"effort must be one of {', '.join(EFFORTS)} — got {effort!r}")
    command = [executable(), "-p", text, "--system-prompt", system, "--model", model,
               *(("--effort", effort) if effort else ()), *BASE_ARGS]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              check=False,
                              # Without this `claude` waits 3s for piped input it will never
                              # get, warns, and has been seen to exit 1.
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"`claude` didn't answer within {timeout}s for {what} — ask for "
                           f"less at a time.") from None
    if done.returncode != 0:
        detail = (done.stderr.strip() or done.stdout.strip() or "no output")[:400]
        raise RuntimeError(f"`claude` exited {done.returncode}: {detail}")
    try:
        payload = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"couldn't parse `claude` output: {done.stdout[:400]}") from None
    if payload.get("is_error"):
        raise RuntimeError(f"claude: {payload.get('result') or payload.get('subtype')}")
    return (payload.get("result", ""), payload.get("usage") or {},
            payload.get("total_cost_usd") or 0.0)
