"""The user's Migaku vocabulary, read straight off the disk.

Migaku Memory keeps its whole word list client-side, in IndexedDB. Chrome stores that as a
gzipped SQLite database inside the profile, so this finds the blob, inflates it, and queries
the WordList table. No API, no token, no login — the only requirement is that Migaku has been
open in Chrome recently enough to have synced.

**Migaku keeps that database in TWO origins** — the study.migaku.com site and the extension —
and either can freeze while the other keeps syncing. So both are always candidates and the
freshest wins *by the data's own clock* (`MAX(mod)`, Migaku's sync cursor as the tiebreak):
never by file size, never by trusting one origin. This module used to glob the site origin
alone, and on 2026-08-17 that was measured costing 736 German known words — the site blob had
been frozen since 2026-07-29 while the extension was current to 2026-08-14, and every CL
computed from it was understated by ~9% of the vocabulary. Picking, never merging: merging two
snapshots of one database would resurrect rows the fresher one has already deleted.

`norm()` is the piece everything else depends on: Migaku stores German dictForms lowercased
("abend", "afrika") while the tokenizer returns them capitalised ("Abend"), so every comparison
must run BOTH sides through the same normalisation or the match silently never happens.
"""

from __future__ import annotations

import datetime
import glob
import os
import sqlite3
import sys
import unicodedata
import zlib

# Chrome names each origin's storage directory after the origin. Migaku's database exists
# under both of these; which one is current depends on which side synced last, so both are
# always candidates and freshness decides (see the module docstring).
MIGAKU_EXTENSION_ID = "dmeppfcidcpcocleneopiblmpnbokhep"
MIGAKU_ORIGINS = {
    "site": "https_study.migaku.com_0",
    "extension": f"chrome-extension_{MIGAKU_EXTENSION_ID}_0",
}
CHROME_DIR = "~/Library/Application Support/Google/Chrome"

MIN_WORDS = 1000       # below this the read is broken, not the vocabulary small
STALE_DAYS = 30        # warn if the list has not synced in this long


def norm(word: str) -> str:
    """The one normalisation used on both sides of every word comparison.

    casefold() rather than lower() (German "Straße" -> "strasse"), and NFC so that a
    precomposed "ü" and a "u" + combining diaeresis compare equal.
    """
    return unicodedata.normalize("NFC", word).strip().casefold()


# ----------------------------------------------------------------- the blob ---
def _blob_candidates() -> list[tuple[str, str]]:
    """One candidate database per (origin, profile): the largest file in that origin's
    IndexedDB blob storage.

    Blob storage holds one file per stored Blob; Migaku stores its whole word database as a
    single gzipped one, so *within an origin* "largest" picks it out reliably — but "largest
    across origins" would pick by file size what must be picked by freshness, which is
    `_source()`'s job. Every Chrome profile is searched, not just Default — on a multi-profile
    Chrome the data is rarely in Default.
    """
    candidates = []
    for kind, origin in MIGAKU_ORIGINS.items():
        pattern = os.path.join(os.path.expanduser(CHROME_DIR), "*", "IndexedDB",
                               f"{origin}.indexeddb.blob")
        for blob_dir in glob.glob(pattern):
            blobs = [(os.path.getsize(f), f)
                     for f in glob.glob(os.path.join(blob_dir, "**", "*"), recursive=True)
                     if os.path.isfile(f)]
            if blobs:
                candidates.append((kind, max(blobs)[1]))
    if not candidates:
        raise RuntimeError(
            "No Migaku data in any Chrome profile. Log in at https://study.migaku.com in "
            "Chrome and open your word list once.")
    return candidates


def _inflate(path: str) -> bytes:
    """Chrome wraps the stored Blob in its own header, then gzip. Skip to the gzip magic and
    inflate what follows (raw deflate — the 10-byte gzip header is fixed-size)."""
    with open(path, "rb") as f:
        data = f.read()
    start = data.find(b"\x1f\x8b")
    if start < 0:
        raise RuntimeError(f"Migaku's Chrome blob isn't gzipped as expected: {path}")
    return zlib.decompressobj(-zlib.MAX_WBITS).decompress(data[start + 10:])


def _freshness(sqlite_bytes: bytes) -> tuple[int, int]:
    """(MAX(mod) over every row, Migaku's sync cursor) — one candidate's age by its own clock.

    MAX(mod) over the whole WordList rather than a file mtime: Chrome rewrites a blob for its
    own reasons, and a deletion is activity too. The sync cursor breaks a tie; an older bundle
    may have no LocalSync table at all, which must degrade rather than raise.
    """
    con = sqlite3.connect(":memory:")
    con.deserialize(sqlite_bytes)
    try:
        (max_mod,) = con.execute("SELECT MAX(mod) FROM WordList").fetchone()
        try:
            (version,) = con.execute(
                "SELECT lastSyncedServerVersion FROM LocalSync LIMIT 1").fetchone()
        except sqlite3.Error:
            version = None
        return max_mod or 0, version if version is not None else -1
    finally:
        con.close()


# The chosen source, memoized per process: known_words() and vocabulary() are called back to
# back, and weighing every candidate twice would inflate ~30 MB for no new answer.
_SOURCE: dict | None = None


def _source() -> dict:
    """The freshest readable candidate, with its inflated bytes.

    A candidate that will not inflate or query is skipped with a warning rather than raising:
    the other origin may be perfectly fine, and a broken loser must not sink the run.
    """
    global _SOURCE
    if _SOURCE is None:
        sources = []
        for kind, path in _blob_candidates():
            try:
                sqlite_bytes = _inflate(path)
                max_mod, version = _freshness(sqlite_bytes)
            except Exception as exc:  # noqa: BLE001 — the other origin may still be good
                print(f"migaku: skipping unreadable {kind} blob ({exc})", file=sys.stderr)
                continue
            sources.append({"kind": kind, "path": path, "sqlite": sqlite_bytes,
                            "max_mod": max_mod, "sync_version": version,
                            "profile": path.split("/Chrome/")[-1].split("/")[0]})
        if not sources:
            raise RuntimeError(
                "Migaku's data in Chrome is unreadable in every origin. Open Migaku in Chrome "
                "so it rewrites its database.")
        _SOURCE = max(sources, key=lambda s: (s["max_mod"], s["sync_version"]))
    return _SOURCE


def _rows(sqlite_bytes: bytes, lang: str) -> list[tuple[str, str, int]]:
    """(dictForm, knownStatus, mod) for one language, straight out of the inflated DB.

    deserialize() attaches the bytes as an in-memory database, so the ~40 MB never touches
    the filesystem.
    """
    con = sqlite3.connect(":memory:")
    con.deserialize(sqlite_bytes)
    try:
        return con.execute(
            "SELECT dictForm, knownStatus, mod FROM WordList WHERE del = 0 AND language = ?",
            (lang,)).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------- public API ---
def known_words(lang: str = "de") -> tuple[set[str], dict]:
    """({normalised dictForms the user knows}, meta).

    "Known" is Migaku's own KNOWN state. Everything else — UNKNOWN, LEARNING, IGNORED, and
    words absent from the list entirely — counts as not known.
    """
    source = _source()
    path = source["path"]
    rows = _rows(source["sqlite"], lang)
    if not rows:
        raise RuntimeError(f"Migaku has no {lang!r} words at all (read {path})")

    known = {norm(w) for w, status, _ in rows if status == "KNOWN"}
    if len(known) < MIN_WORDS:
        raise RuntimeError(
            f"only {len(known)} known {lang!r} words in Migaku — that is too few to be real, "
            f"the read is probably broken (blob {path})")

    # This language's own newest change, not the database's: a Japanese-only week must still
    # read as a stale German list, which is the thing the warning exists to catch.
    newest = datetime.datetime.fromtimestamp(max(m for _, _, m in rows) / 1000)
    meta = {
        "profile": source["profile"],
        "origin": source["kind"],
        "known": len(known),
        "vocab": len({norm(w) for w, _, _ in rows}),
        "newest": newest,
        "stale": (datetime.datetime.now() - newest).days > STALE_DAYS,
    }
    return known, meta


def vocabulary(lang: str = "de") -> set[str]:
    """Every word Migaku has a record of, whatever its state. Used to check that the
    tokenizer's lemmas line up with the vocabulary the word list was built from."""
    return {norm(w) for w, _, _ in _rows(_source()["sqlite"], lang)}


if __name__ == "__main__":
    words, info = known_words()
    print(f"migaku: known={info['known']} vocab={info['vocab']} "
          f"profile={info['profile']!r} origin={info['origin']} "
          f"newest={info['newest']:%Y-%m-%d}{'  STALE' if info['stale'] else ''}")
    print("sample:", sorted(words)[:12])
