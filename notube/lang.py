"""Fill the Channels "Lang" field (content language) with Claude Haiku.

Classification uses the logged-in `claude` CLI (subscription, no API key) and validates
the result against an allowed name set. To reach full coverage the channel set includes
two catch-all buckets (Music = no speech, Mixed = polyglot or undeterminable), and the
model is fed extra signals: channel country, default language, and recent video titles —
so it almost never has to fall back to a catch-all.

`run(cids)` classifies channels (rows with a YT Channel ID) that have an empty Lang —
`cids` restricts to specific channels. `run_podcasts()` classifies Pocket Casts rows
(those with a PC ID); there the choice is FORCED to Japanese or German (the user's
collection is Japanese/German), so a podcast is never left empty.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

from notube.config import CHANNELS_DB, LANGS
from notube.notion import call, client, query_rows
from notube.youtube import client as yt_client
from notube.youtube import fetch_channels

MODEL = "claude-haiku-4-5-20251001"
_TMPDIR = tempfile.gettempdir()  # run claude outside the project so it picks up no project settings
# The claude CLI is not on PATH under cron/systemd/bare-ssh (it lives in ~/.local/bin).
# Resolve it once: NOTUBE_CLAUDE_BIN override -> PATH -> the standard install location.
_CLAUDE_BIN = (os.environ.get("NOTUBE_CLAUDE_BIN") or shutil.which("claude")
               or os.path.expanduser("~/.local/bin/claude"))

RULES = """You classify a YouTube channel into ONE content-language name. You MUST output one.

Output EXACTLY one of these names, nothing else (by CONTENT language, not the UI):
English Japanese Korean Chinese Russian French Spanish German Italian Portuguese Thai
Vietnamese Indonesian Hindi Arabic Turkish Polish Dutch Ukrainian Swedish Music Mixed

Two catch-all buckets, use ONLY when no single language fits:
Music = no spoken language at all (pure music, ambient, dance, instrumental).
Mixed = polyglot, OR a real spoken language NOT in the list above (e.g. Latin, Greek),
        OR you cannot identify any single content language.

Rules:
- Language-LEARNING channels: use the language being TAUGHT (the target), NOT the teacher's.
  A channel teaching Japanese is Japanese even if narrated in English.
- ASMR / talking / vlog channels: the language the creator SPEAKS.
- Script is a strong signal: Hangul -> Korean, kana/kanji -> Japanese, Cyrillic -> Russian,
  Thai script -> Thai.
- "Channel country" and "Default language" are hints, but CONTENT language wins (a channel
  teaching Japanese from the US is still Japanese).
- NEVER output UNKNOWN. If there is no speech at all, use Music; if it is polyglot or you
  truly cannot tell the single language, use Mixed. But prefer a concrete language whenever
  one fits.

NEVER ask questions and NEVER explain. Output exactly one name.

Examples:
- an English-speaking channel that teaches Japanese -> Japanese
- a channel teaching German, narrated in English -> German
- a Korean ASMR channel (Hangul title, Korean whispering) -> Korean
- a Brazilian ASMR channel (Portuguese speech) -> Portuguese
- a 24/7 instrumental music stream, no speech -> Music

Respond with ONLY the name (e.g. Japanese)."""

# Podcasts are forced into exactly one of these two (the collection is Japanese/German).
PODCAST_LANGS = {"Japanese", "German"}
_JP_SCRIPT = re.compile("[\u3040-\u30ff\u4e00-\u9fff]")  # kana / kanji — Japanese-vs-German tiebreak

PODCAST_RULES = """You classify a podcast as Japanese or German. You MUST choose one.

Output EXACTLY one name, nothing else:
Japanese German

Rules:
- Japanese = Japanese content; German = German content.
- Language-LEARNING podcasts: use the language being TAUGHT (the target), NOT the host's.
  A podcast teaching Japanese is Japanese even if hosted in English.
- Japanese script (kana/kanji) -> Japanese; German words -> German.
- This collection is almost entirely Japanese- or German-related. Even if the podcast is in
  another language (English, Russian, ...) or unclear, pick the CLOSER of the two from any
  hint (topic, host names, region). NEVER output anything other than Japanese or German.

NEVER ask questions and NEVER explain. Output exactly one name (Japanese or German)."""


def _uploads_playlist(row: dict) -> str | None:
    m = re.search(r"[?&]list=([\w-]+)", row.get("YT Uploads") or "")
    if m:
        return m.group(1)
    cid = row.get("YT Channel ID") or ""
    return "UU" + cid[2:] if cid.startswith("UC") else None


def _recent_titles(yt, row: dict, n: int = 3) -> list[str]:
    pl = _uploads_playlist(row)
    if not pl:
        return []
    try:
        resp = yt.playlistItems().list(part="snippet", playlistId=pl, maxResults=n).execute(num_retries=2)
    except Exception:  # noqa: BLE001 — no titles must not break classification
        return []
    return [it["snippet"].get("title", "") for it in resp.get("items", [])][:n]


def _claude(system_prompt: str, user_text: str) -> str | None:
    """Run the logged-in claude CLI (subscription, no API key); return the stripped
    result text, or None on timeout / non-zero exit / model error. On failure logs the
    reason to stderr so silent skipped rows can be diagnosed from the cron stdout."""
    try:
        proc = subprocess.run(
            [_CLAUDE_BIN, "-p", "--safe-mode", "--model", MODEL, "--output-format", "json",
             "--system-prompt", system_prompt],
            input=user_text, capture_output=True, text=True, timeout=120, cwd=_TMPDIR,
        )
    except subprocess.TimeoutExpired:
        print(f"[lang] claude CLI timed out after 120s (model={MODEL})", file=sys.stderr, flush=True)
        return None
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-300:].replace("\n", " ")
        print(f"[lang] claude CLI exit={proc.returncode} (model={MODEL}): {tail}", file=sys.stderr, flush=True)
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(f"[lang] claude CLI returned non-JSON (model={MODEL}): {proc.stdout[:300]!r}", file=sys.stderr, flush=True)
        return None
    if data.get("is_error"):
        print(f"[lang] claude CLI is_error=true (model={MODEL}): {str(data.get('result'))[:300]}", file=sys.stderr, flush=True)
        return None
    return (data.get("result") or "").strip()


def classify(name: str, desc: str, keywords: str, titles: list[str],
             country: str = "", default_lang: str = "") -> str | None:
    """One allowed name (a language or the Music/Mixed catch-all), or None only on a CLI error."""
    titles_block = "\n".join(f"- {t}" for t in titles) if titles else "(none)"
    user_text = (f"Channel name: {name}\nKeywords: {keywords}\n"
                 f"Channel country: {country or '(unknown)'}\n"
                 f"Default language: {default_lang or '(unknown)'}\n"
                 f"Recent video titles:\n{titles_block}\nDescription: {desc}").strip()
    raw = _claude(RULES, user_text)
    return raw if raw in LANGS else None


def classify_podcast(name: str, desc: str) -> str | None:
    """Forced binary: always "Japanese" or "German" (the collection is Japanese/German and
    the user wants no empty podcasts). None only on a CLI error, so the row is retried."""
    user_text = f"Podcast name: {name}\nDescription: {desc}".strip()
    raw = _claude(PODCAST_RULES, user_text)
    if raw is None:
        return None
    if raw in PODCAST_LANGS:
        return raw
    return "Japanese" if _JP_SCRIPT.search(f"{name} {desc}") else "German"  # disobeyed: script tiebreak


def _channel_meta(ch: dict) -> tuple[str, str]:
    """(country, defaultLanguage) from a channel object — empty strings if absent."""
    sn = ch.get("snippet", {})
    country = sn.get("country") or ch.get("brandingSettings", {}).get("channel", {}).get("country") or ""
    return country, sn.get("defaultLanguage") or ""


def run(cids=None, *, dry_run: bool = False, workers: int = 4) -> dict:
    """Classify channels (rows with a YT Channel ID) that have an empty Lang, feeding the
    model the channel's country, default language and recent video titles. `cids` restricts
    to specific channel ids. Returns {"set": n_written, "skipped": n_left_empty}."""
    rows = query_rows(CHANNELS_DB)
    todo = [r for r in rows
            if (r.get("YT Channel ID") or "").strip()
            and not r.get("Lang")
            and (r.get("_title") or "").strip()]
    if cids is not None:
        cids = set(cids)
        todo = [r for r in todo if (r.get("YT Channel ID") or "") in cids]
    if not todo:
        return {"set": 0, "skipped": 0}

    yt = yt_client()  # serial: httplib2 is not thread-safe
    channels = fetch_channels(yt, [(r.get("YT Channel ID") or "").strip() for r in todo])
    titles_by = {r["_id"]: _recent_titles(yt, r, n=12) for r in todo}
    meta_by = {r["_id"]: _channel_meta(channels.get((r.get("YT Channel ID") or "").strip(), {}))
               for r in todo}

    stats = {"set": 0, "skipped": 0}
    lock = threading.Lock()

    def handle(r: dict) -> None:
        country, default_lang = meta_by[r["_id"]]
        code = classify(r.get("_title") or "", r.get("YT Description") or "",
                        r.get("YT Keywords") or "", titles_by[r["_id"]], country, default_lang)
        if code is None:
            with lock:
                stats["skipped"] += 1
            return
        if not dry_run:
            call(client().pages.update, page_id=r["_id"], properties={"Lang": {"select": {"name": code}}})
        with lock:
            stats["set"] += 1

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(handle, todo))
    return stats


def run_podcasts(*, dry_run: bool = False, workers: int = 4) -> dict:
    """Classify podcast rows (those with a PC ID) that have an empty Lang, from the
    title + PC Description. Only JP or DE are written; anything else stays empty.
    No YouTube needed. Returns {"set": n_written, "skipped": n_left_empty}."""
    rows = query_rows(CHANNELS_DB)
    todo = [r for r in rows
            if (r.get("PC ID") or "").strip()
            and not r.get("Lang")
            and (r.get("_title") or "").strip()]
    if not todo:
        return {"set": 0, "skipped": 0}

    stats = {"set": 0, "skipped": 0}
    lock = threading.Lock()

    def handle(r: dict) -> None:
        code = classify_podcast(r.get("_title") or "", r.get("PC Description") or "")
        if code is None:
            with lock:
                stats["skipped"] += 1
            return
        if not dry_run:
            call(client().pages.update, page_id=r["_id"], properties={"Lang": {"select": {"name": code}}})
        with lock:
            stats["set"] += 1

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(handle, todo))
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Classify empty Lang fields in Channels via Haiku.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--podcasts", action="store_true", help="classify Pocket Casts rows (JP/DE) instead of channels")
    args = ap.parse_args()
    stats = (run_podcasts if args.podcasts else run)(dry_run=args.dry_run)
    kind = "podcasts" if args.podcasts else "channels"
    print(f"lang ({kind}): set={stats['set']} left_empty={stats['skipped']} (dry_run={args.dry_run})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
