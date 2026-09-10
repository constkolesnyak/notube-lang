"""Tag dead YouTube channels in Channels with "dead url" in the multi-select "Flags" field.

Conservative hybrid check (avoids false positives):
  1) a fast HTTP GET of the channel page;
  2) confirm "dead" / ambiguous answers with yt-dlp.
A channel is tagged only when clearly gone; anything uncertain (429 / network /
bot-check) is left alone. Not part of the daily sync — run it occasionally.

    uv run python -m notube.yt_dead --dry-run --limit 20   # check, write nothing
    uv run python -m notube.yt_dead                          # full run
    uv run python -m notube.yt_dead --recheck                # re-check the ambiguous ones
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from notube import common  # noqa: F401  (importing forces IPv4 + socket timeout)
from notube.common import run_path
from notube.config import CHANNELS_DB
from notube.notion import call, client, query_rows, resolve_data_source_id
from notube.youtube import YT_RE

FIELD = "Flags"  # multi-select on Channels; created on first run if missing
TAG = "dead url"
PROGRESS = run_path("yt_dead_progress.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
COOKIES = {"SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"}  # skip EU consent redirect
DEAD_MARKERS = ("has been terminated", "isn't available", "isn’t available",
                "doesn't exist", "this account has been")
CANONICAL_RE = re.compile(r'rel="canonical" href="https://www\.youtube\.com/(channel/|@)', re.I)
YTDLP_DEAD = ("does not exist", "not found", "404", "could not find", "unable to recognize",
              "no longer available", "has been terminated", "account associated",
              "this channel does not", "is not available", "was removed", "removed because",
              "violated", "account has been closed")
YTDLP_AMBIG = ("sign in to confirm", "429", "rate", "blocked", "captcha", "consent")
HTTP_THROTTLE = 0.4


def ensure_field(ds_id: str) -> None:
    ds = call(client().data_sources.retrieve, data_source_id=ds_id)
    if FIELD in ds.get("properties", {}):
        return
    call(client().data_sources.update, data_source_id=ds_id,
         properties={FIELD: {"multi_select": {"options": [{"name": TAG}]}}})


def tag_dead(page_id: str, existing: list | None) -> None:
    names = list(existing or [])
    if TAG not in names:
        names.append(TAG)
    call(client().pages.update, page_id=page_id,
         properties={FIELD: {"multi_select": [{"name": n} for n in names]}})


def channel_url(link: str | None) -> str | None:
    m = YT_RE.search(link or "")
    return f"https://www.youtube.com/{m.group(1)}" if m else None


def http_check(http: httpx.Client, url: str) -> str:
    """alive | dead | ambiguous (dead / soft-404 is confirmed later by yt-dlp)."""
    try:
        r = http.get(url)
    except Exception:  # noqa: BLE001
        return "ambiguous"
    if r.status_code == 404:
        return "dead"
    if r.status_code == 200:
        if "consent" in (r.url.host or ""):
            return "ambiguous"
        if any(m in r.text.lower() for m in DEAD_MARKERS):
            return "dead"
        if CANONICAL_RE.search(r.text):
            return "alive"
        return "ambiguous"  # 200 without canonical = soft-404
    return "ambiguous"


def ytdlp_check(url: str) -> str:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "skip_download": True, "socket_timeout": 20}
    try:
        with YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=False, process=False)
        return "alive"
    except DownloadError as e:
        msg = str(e).lower()
        if any(s in msg for s in YTDLP_AMBIG):
            return "ambiguous"
        if any(s in msg for s in YTDLP_DEAD):
            return "dead"
        return "ambiguous"
    except Exception:  # noqa: BLE001
        return "ambiguous"


def verdict(http: httpx.Client, url: str) -> tuple[str, str]:
    h = http_check(http, url)
    if h == "alive":
        return "alive", "http-alive"
    y = ytdlp_check(url)
    if h == "dead":
        return ("alive", "ytdlp-override") if y == "alive" else ("dead", f"http-dead+ytdlp-{y}")
    if y == "dead":
        return "dead", "ytdlp-dead"
    if y == "alive":
        return "alive", "ytdlp-alive"
    return "ambiguous", "both-ambiguous"


def main() -> int:
    ap = argparse.ArgumentParser(description="Tag dead YouTube channels in Channels.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--recheck", action="store_true", help="re-check ambiguous results")
    args = ap.parse_args()

    ds_id = resolve_data_source_id(CHANNELS_DB)
    if not args.dry_run:
        ensure_field(ds_id)

    rows = query_rows(CHANNELS_DB)
    yt_rows = [(r, channel_url(r.get("Link"))) for r in rows]
    yt_rows = [(r, u) for r, u in yt_rows if u]
    progress = {} if args.dry_run else (json.loads(PROGRESS.read_text()) if PROGRESS.exists() else {})
    print(f"rows: {len(rows)} | youtube channels: {len(yt_rows)}")

    http = httpx.Client(headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                        cookies=COOKIES, follow_redirects=True, timeout=15.0)
    counts = {"dead": 0, "alive": 0, "ambiguous": 0, "skipped": 0}
    processed = 0
    for r, url in yt_rows:
        pid = r["_id"]
        prev = progress.get(pid)
        if prev and (prev["status"] in ("dead", "alive") or (prev["status"] == "ambiguous" and not args.recheck)):
            counts["skipped"] += 1
            continue
        if args.limit is not None and processed >= args.limit:
            break
        status, reason = verdict(http, url)
        progress[pid] = {"status": status, "url": url, "name": r.get("_title", ""), "reason": reason}
        counts[status] += 1
        processed += 1
        if status == "dead":
            print(f"  DEAD  {r.get('_title', '')!r}  {url}  ({reason})")
            if not args.dry_run:
                tag_dead(pid, r.get(FIELD))
        if processed % 25 == 0 and not args.dry_run:
            PROGRESS.write_text(json.dumps(progress, ensure_ascii=False))
        time.sleep(HTTP_THROTTLE)

    if not args.dry_run:
        PROGRESS.write_text(json.dumps(progress, ensure_ascii=False))
    http.close()
    print(f"dead={counts['dead']} alive={counts['alive']} ambiguous={counts['ambiguous']} "
          f"skipped={counts['skipped']} (dry_run={args.dry_run})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
