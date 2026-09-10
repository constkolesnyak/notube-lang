"""Tokenize text the way Migaku itself does, via the migaku-tokenizer sidecar.

Comprehension only means something if the words being counted are segmented and lemmatised
the same way the word list was built. migaku-tokenizer boots Migaku's own analyzer bundle
headlessly in Node, so "gesehen" becomes "sehen" and matches the stored dictForm — a
general-purpose lemmatizer would produce plausible but subtly different lemmas, and a
comprehension score that is quietly a few points wrong.

Nothing here needs doing by hand: `install()` clones, npm-installs and snapshots, and the
Sidecar calls it. It is idempotent, so the cost is paid once.

    uv run python -m notube.migaku_tok --setup --code de     # explicit one-off
    uv run python -m notube.migaku_tok --code de --text "..."  # smoke test

THE SNAPSHOT TRAP: the tool copies the Migaku Chrome extension into a snapshot dir (so a
Chrome auto-update cannot pull the files out from under a running sidecar), and its
SKIP_CORE_FILE regex in extpath.mjs deliberately strips every *non-Japanese* dictionary.
setup_snapshot() therefore builds the snapshot itself, keeping the languages asked for, and
writes the .complete marker that makes extpath.mjs skip its own stripping copy. Delete the
snapshot dir and you silently get a setup with no dictionary for your language and a
confusing failure much further downstream.

Japanese is the exception: it parses with the kuromoji .bin files and has no ja.json, which is
why the "did the dictionary make it?" check skips it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path

import httpx

from notube import common  # noqa: F401  (importing forces IPv4 + socket timeout)
from notube.common import RUN_DIR, retry

TOK_DIR = RUN_DIR / "mimi-lab" / "tools" / "migaku-tokenizer"
SNAPSHOT_DIR = RUN_DIR / "migaku-ext-snapshot"
EXT_ID = "dmeppfcidcpcocleneopiblmpnbokhep"
CHROME_EXTENSIONS = Path("~/Library/Application Support/Google/Chrome").expanduser()

HOST, PORT = "127.0.0.1", 8788
URL = f"http://{HOST}:{PORT}"
BOOT_TIMEOUT = 180          # the analyzer bundle is ~6 MB of Kotlin/JS plus dictionaries
BATCH = 400                 # lines per POST: small enough to show progress, big enough to be cheap


# ------------------------------------------------------------------- setup ---
def _live_ext() -> Path:
    """The newest installed Migaku extension version directory."""
    found = sorted(CHROME_EXTENSIONS.glob(f"*/Extensions/{EXT_ID}/*"))
    usable = [p for p in found if (p / "assets").is_dir() and (p / "core").is_dir()]
    if not usable:
        raise RuntimeError(
            f"No Migaku extension under {CHROME_EXTENSIONS}/*/Extensions/{EXT_ID}/. "
            "Install the Migaku Early-Access Chrome extension.")
    return usable[-1]


def _snapshot_codes(dst: Path) -> set[str]:
    """The language codes a finished snapshot was built for, or empty if there is none."""
    marker = dst / ".complete"
    if not marker.exists():
        return set()
    try:
        return set(json.loads(marker.read_text())["codes"])
    except (ValueError, KeyError):
        return set()      # a marker from before this was recorded: rebuild to be sure


def setup_snapshot(codes: Iterable[str] = ("de",), *, force: bool = False) -> Path:
    """Copy the live extension into the snapshot, keeping `codes`' dictionaries.

    Mirrors what extpath.mjs's snapshot() does, except its filter keeps only ja.* — so we do
    the copy ourselves and write .complete, which makes resolveExt() load the snapshot as-is.

    Each language's dictionary is large (es.json alone is 334 MB), so only the requested ones
    are kept and the marker records which. Asking for a language the snapshot lacks rebuilds it
    for the union, which is what makes adding a second language a one-command affair.
    """
    live = _live_ext()
    dst = SNAPSHOT_DIR / live.name
    have = _snapshot_codes(dst)
    want = set(codes) | (set() if force else have)
    if have >= want and not force:
        return dst

    shutil.rmtree(dst, ignore_errors=True)
    (dst / "core").mkdir(parents=True)
    shutil.copytree(live / "assets", dst / "assets")
    shutil.copy2(live / "manifest.json", dst / "manifest.json")
    # Everything in core/ except other languages' dictionaries: the kuromoji .bin files
    # (which are what Japanese parses with — there is no ja.json), sql-wasm.wasm,
    # models_light/, user_data/ ... plus <code>.* for each language we want.
    keep = tuple(f"{c}." for c in want)
    for item in (live / "core").iterdir():
        if item.is_dir():
            shutil.copytree(item, dst / "core" / item.name)
        elif item.suffix in (".json", ".db") and not item.name.startswith(keep):
            continue
        else:
            shutil.copy2(item, dst / "core" / item.name)
    # Japanese is the one language with no dictionary blob of its own, so only check the rest.
    missing = [c for c in want if c != "ja" and not list((dst / "core").glob(f"{c}.*"))]
    if missing:
        raise RuntimeError(f"snapshot built without dictionaries for {missing} — "
                           f"check {live}/core")
    (dst / ".complete").write_text(json.dumps({"codes": sorted(want), "built": time.time()}))
    return dst


REPO = "https://github.com/pufit/mimi-lab.git"


def install(codes: Iterable[str] = ("de",)) -> Path:
    """Clone migaku-tokenizer and build the extension snapshot. Idempotent.

    A sparse, blobless clone: mimi-lab is a whole application and we want one directory of it.
    """
    root = RUN_DIR / "mimi-lab"
    if not (root / ".git").is_dir():
        shutil.rmtree(root, ignore_errors=True)
        root.parent.mkdir(parents=True, exist_ok=True)
        _sh(["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", REPO, str(root)])
        _sh(["git", "-C", str(root), "sparse-checkout", "set", "tools/migaku-tokenizer"])
    if not (TOK_DIR / "node_modules").is_dir():
        _sh(["npm", "ci"], cwd=TOK_DIR)
    return setup_snapshot(codes)


def _sh(cmd: list[str], cwd: Path | None = None) -> None:
    print(f"  $ {' '.join(cmd[:4])}...", flush=True)
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}):\n{proc.stderr[-2000:]}")


def _env() -> dict:
    """Environment for the Node sidecar: point it at the real profile and our snapshot."""
    live = _live_ext()
    return os.environ | {
        "MIGAKU_EXT_ID": EXT_ID,
        "MIGAKU_CDP_PROFILE": str(live.parent),   # the Extensions/<id> dir holding version dirs
        "MIGAKU_EXT_VERSION": live.name,          # the raw dir name, "_0" suffix included
        "MIGAKU_EXT_SNAPSHOT_DIR": str(SNAPSHOT_DIR),
        "MIGAKU_TOK_PORT": str(PORT),
        "MIGAKU_TOK_HOST": HOST,
    }


# ----------------------------------------------------------------- sidecar ---
def _healthy() -> bool:
    try:
        return httpx.get(f"{URL}/health", timeout=3).status_code == 200
    except Exception:  # noqa: BLE001 — any failure means "not up yet"
        return False


class Sidecar:
    """Runs `node server.mjs` for the duration of a with-block.

    Attaches to an already-running sidecar instead of starting a second one, so a
    LaunchAgent-managed instance (or a leftover from a previous run) is reused.
    """

    def __init__(self, codes: Iterable[str] = ("de",)) -> None:
        self.codes = tuple(codes)
        self.proc: subprocess.Popen | None = None
        self.log = RUN_DIR / "tok-server.log"

    def __enter__(self) -> Sidecar:
        if _healthy():
            return self
        install(self.codes)      # idempotent: clone + npm ci + snapshot, only if needed
        self.log.parent.mkdir(parents=True, exist_ok=True)
        handle = self.log.open("w")
        self.proc = subprocess.Popen(["node", "server.mjs"], cwd=TOK_DIR, env=_env(),
                                     stdout=handle, stderr=subprocess.STDOUT)
        deadline = time.time() + BOOT_TIMEOUT
        while time.time() < deadline:
            if _healthy():
                return self
            if self.proc.poll() is not None:
                raise RuntimeError(f"tokenizer sidecar died on startup; see {self.log}\n"
                                   f"{self.log.read_text()[-2000:]}")
            time.sleep(1)
        self.__exit__(None, None, None)
        raise RuntimeError(f"tokenizer sidecar did not come up in {BOOT_TIMEOUT}s; see {self.log}")

    def __exit__(self, *exc) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None


# ---------------------------------------------------------------- tokenize ---
def _post(lines: list[str], lang: str) -> list[list[dict]]:
    r = httpx.post(f"{URL}/tokenize", json={"lang": lang, "lines": lines}, timeout=600)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"tokenizer: {data['error']}")
    tokens = data.get("tokens")
    if not isinstance(tokens, list) or len(tokens) != len(lines):
        raise RuntimeError(f"tokenizer returned {len(tokens or [])} results for {len(lines)} lines")
    return tokens


def tokenize(lines: list[str], lang: str) -> list[list[dict]]:
    """One token list per input line: {surface, dictForm, reading, pos, unrecognized}.

    Serial by design — the sidecar tokenizes in a single process, so concurrent requests buy
    memory pressure and no speed.
    """
    out: list[list[dict]] = []
    for i in range(0, len(lines), BATCH):
        chunk = lines[i:i + BATCH]
        out.extend(retry(lambda c=chunk: _post(c, lang), tries=3))
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="migaku-tokenizer sidecar helper.")
    ap.add_argument("--setup", action="store_true",
                    help="clone, install and snapshot, then exit")
    ap.add_argument("--force", action="store_true", help="rebuild the snapshot from scratch")
    ap.add_argument("--code", default="de", help="ISO language code (default de)")
    ap.add_argument("--text", default="Ich habe gestern einen sehr schönen Film gesehen.")
    args = ap.parse_args()

    if args.setup:
        install([args.code]) if not args.force else setup_snapshot([args.code], force=True)
        dst = SNAPSHOT_DIR / _live_ext().name
        size = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
        print(f"snapshot: {dst} ({size / 1e6:.0f} MB, "
              f"languages {', '.join(sorted(_snapshot_codes(dst)))})")
        return 0

    with Sidecar([args.code]):
        for tok in tokenize([args.text], args.code)[0]:
            if any(c.isalpha() for c in tok["surface"]):
                print(f"  {tok['surface']:16} -> {tok['dictForm']:16} pos={tok.get('pos')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
