"""Download subtitles for the freshest normal videos of one language's YT channels.

Selects the channels of the matching Notion Channels view — Type empty, Tags contains "Input",
Lang == the chosen language, a YouTube channel id — except those dropped or skipped, then for
each takes its N_VIDEOS freshest NORMAL uploads (Shorts, clips and streams skipped) and saves
the best subtitle track in that language (manual if present, else auto) as a .srt under
run/subs/.

The language comes from config.CL_LANGS: its Channels "Lang" value picks the channels, its ISO
code picks the caption tracks. Nothing here is German-specific.

Every video looked at is recorded in run/subs_index_<code>.json, so its classification survives
to the next run and cl.py can reuse the whole selection without re-extracting anything.

YouTube serves .srt and .vtt directly in the caption track list, so there is no ffmpeg
conversion; we just fetch the chosen track's URL. Run: `uv run python -m notube.subs`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from pathlib import Path

import httpx

from notube import common  # noqa: F401  (importing forces IPv4 + socket timeout)
from notube.common import RUN_DIR, retry
from notube.config import CHANNELS_DB, CL_DEFAULT_LANG, CL_LANGS
from notube.notion import query_rows

# --------------------------------------------------------------- settings ---
SKIP_STATUSES = {"Dropped", "Skip"}      # every other channel in the view is processed
VIEW_TAG = "Input"                       # the view's Tags filter
N_VIDEOS = 3                             # freshest NORMAL videos the Subs verdict is read from
# ...but the SAMPLE keeps going. Three videos is a rule about recency, not a budget: a channel
# that posts eight-minute videos gave cl.py 142 words where the median channel gave 7,300, and
# 18 channels ended up with no CL at all because their three freshest were short — while the
# scan window held 30 videos nobody looked at. So older videos are added until there is enough
# text, and only until then. Measured over 1,215 subtitle files, a raw .srt costs 12.1 bytes
# per token (p10 11.1, p90 13.4), so this is cl.MIN_TOKENS with the p90 ratio and a margin;
# a normally-sampled channel clears it on the first three and never notices.
SAMPLE_BYTES = 28_000
MAX_VIDEOS = 10                          # ...and never chase it past here
SCAN_MAX = 30                            # newest entries of the Videos tab to consider
SHORT_MAX_SEC = 180                      # a Short is vertical AND <= this many seconds
MIN_VIDEO_SEC = 60                       # anything briefer is a clip, not a video
# A normal video is neither a Short nor a stream. Note that YouTube reports a premiere that
# has finished as "was_live" too, so this errs toward excluding a few ordinary uploads.
STREAM_STATUSES = {"is_live", "was_live", "post_live", "is_upcoming"}
FMT = "srt"                             # "srt" or "vtt" — YouTube serves both directly
MANUAL_ONLY = False                     # False = fall back to auto-generated captions
OUT_DIR = RUN_DIR / "subs"

# ignore_no_formats_error: we only ever want metadata and the caption track list, but yt-dlp
# still runs format selection and aborts the whole extraction when it cannot satisfy it —
# which YouTube triggers intermittently on perfectly normal videos.
YTDLP_OPTS = {"quiet": True, "no_warnings": True, "skip_download": True,
              "socket_timeout": 20, "retries": 2, "extractor_retries": 1,
              "ignore_no_formats_error": True}


# YouTube answers a bulk run with an escalating ban: HTTP 429, then "Sign in to confirm
# you're not a bot" on every request, for 12 minutes and then longer. Retrying into that is
# the one thing that makes it worse, and a loop that merely records each failure and moves on
# writes off the rest of the queue in minutes — measured twice. So a
# run of consecutive bot-check failures stops the WHOLE run and polls cheaply until access
# comes back, rather than spending the ban on channels it will have to redo anyway.
BLOCK_MARKERS = ("not a bot", "429", "Too Many Requests", "blocked it in your country",
                 "This content isn't available")
BLOCK_STREAK = 4          # consecutive bot-looking failures before believing it
BLOCK_WAIT = 600          # seconds between probes while blocked
BLOCK_MAX_WAITS = 12      # two hours; past that the run stops and says so
_blocked_streak = 0


def looks_blocked(message: str) -> bool:
    return any(m.lower() in message.lower() for m in BLOCK_MARKERS)


def wait_out_block(probe_id: str) -> None:
    """Poll one cheap metadata call until YouTube answers normally again."""
    from yt_dlp import YoutubeDL
    for attempt in range(1, BLOCK_MAX_WAITS + 1):
        print(f"subs: YouTube is refusing ({BLOCK_STREAK} in a row) — pausing "
              f"{BLOCK_WAIT // 60} min, probe {attempt}/{BLOCK_MAX_WAITS}", flush=True)
        time.sleep(BLOCK_WAIT)
        try:
            with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                            "extract_flat": True, "playlistend": 1,
                            "socket_timeout": 20}) as ydl:
                ydl.extract_info(f"https://www.youtube.com/watch?v={probe_id}", download=False)
            print("subs: YouTube is answering again — resuming", flush=True)
            return
        except Exception:  # noqa: BLE001 — still blocked; that is what we are waiting for
            continue
    raise RuntimeError(f"YouTube has refused every request for "
                       f"{BLOCK_MAX_WAITS * BLOCK_WAIT // 3600}h — stopping rather than "
                       f"writing off the rest of the queue")


# ------------------------------------------------------------- selection ---
def _tags(row: dict) -> list[str]:
    t = row.get("Tags") or []
    return t if isinstance(t, list) else [t]


def select_channels(rows: list[dict], lang: str) -> list[dict]:
    """Rows matching one language's YT view filter, minus the statuses not worth fetching."""
    return [r for r in rows
            if r.get("Status") not in SKIP_STATUSES
            and not r.get("Type")
            and VIEW_TAG in _tags(r)
            and r.get("Lang") == lang
            and (r.get("YT Channel ID") or "").strip()]


def channel_videos(channel_id: str) -> list[tuple[str, int]]:
    """Up to SCAN_MAX newest (video id, seconds) from the channel's Videos tab, newest first.

    The Videos tab rather than the uploads playlist, because the uploads playlist mixes Shorts
    in with everything else and a channel that posts Shorts daily buries its real videos: on one
    such channel the newest actual video sat at position 60, so a 30-upload window saw nothing
    but Shorts and concluded the channel had no videos at all. YouTube already separates the two,
    so ask it for the same list a human sees, and get durations in the same flat call.
    """
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": "in_playlist", "playlistend": SCAN_MAX, "socket_timeout": 30}
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        if "does not have a videos tab" in str(exc):
            return []          # a Shorts-only or streams-only channel: genuinely no videos
        raise
    return [(e["id"], int(e.get("duration") or 0))
            for e in (info.get("entries") or []) if e.get("id")]


# --------------------------------------------------------- yt-dlp / subs ---
def extract(url: str) -> dict:
    """Video info via yt-dlp, retried until the response is complete.

    YouTube intermittently (~1 call in 3, measured) answers with a stub: no formats, no
    aspect ratio and an empty caption list, for a video that plainly has all three. Trusting
    one of those silently mislabels the video as "no subtitles". A stub carries nothing
    at all, so it is distinguishable from a video that genuinely has no captions — treat it as
    a transient failure and back off.
    """
    def once() -> dict:
        info = _extract_once(url)
        if not usable(info):
            raise RuntimeError(f"incomplete extraction for {url} "
                               "(no caption list and no geometry)")
        return info

    return retry(once, tries=4, base=2.0)


def usable(info: dict) -> bool:
    """Did the response actually tell us anything?

    A stub answers with neither caption tracks nor geometry. A response carrying captions but
    no geometry is still fine — that is what a signed-in extraction tends to look like, and
    geometry only decides Shorts, which only matters below SHORT_MAX_SEC.
    """
    return bool(info.get("automatic_captions") or info.get("subtitles")) or aspect(info) is not None


def _extract_once(url: str) -> dict:
    """One yt-dlp call; on a bot-check DownloadError, retry once with browser cookies."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
    try:
        with YoutubeDL(YTDLP_OPTS) as ydl:
            return ydl.extract_info(url, download=False)
    except DownloadError:
        opts = dict(YTDLP_OPTS)
        import os
        cookies_file = os.environ.get("NOTUBE_COOKIES_FILE")
        if cookies_file:
            opts["cookiefile"] = cookies_file
        else:
            browser = os.environ.get("NOTUBE_BROWSER", "chrome")
            profile = os.environ.get("NOTUBE_CHROME_PROFILE") or None
            opts["cookiesfrombrowser"] = (browser, profile, None, None)
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)


def aspect(info: dict) -> float | None:
    """width / height, or None if the extraction did not reveal it.

    Not just info["width"]/["height"]: those come from the *selected* format, which yt-dlp
    leaves unset on some extractions — turning "is it vertical?" into "0 > 0" and letting
    Shorts through as normal videos. The format-list fallback must skip storyboards
    (vcodec "none"): those are horizontal thumbnail grids, so a vertical Short reads as 1.78.
    """
    if info.get("aspect_ratio"):
        return info["aspect_ratio"]
    w, h = info.get("width") or 0, info.get("height") or 0
    if not (w and h):
        dims = [(f["width"], f["height"]) for f in (info.get("formats") or [])
                if f.get("width") and f.get("height") and f.get("vcodec") != "none"]
        if dims:
            w, h = max(dims, key=lambda d: d[0] * d[1])
    return w / h if w and h else None


def is_short(info: dict, duration: int = 0) -> bool:
    """A Short: vertical AND at most SHORT_MAX_SEC long.

    Anything longer than SHORT_MAX_SEC cannot be a Short whatever its shape, so the usual case
    never consults the flaky geometry at all. Inside that window an unknown orientation counts
    as a Short: these channels post far more Shorts than 1-3 minute videos, and passing over an
    unverifiable one just moves on to the next upload.
    """
    dur = info.get("duration") or duration
    if not 0 < dur <= SHORT_MAX_SEC:
        return False
    ratio = aspect(info)
    return ratio is None or ratio < 1


def is_stream(info: dict) -> bool:
    """A livestream, a stream VOD, or an upcoming premiere — not a normal upload."""
    return (info.get("live_status") in STREAM_STATUSES
            or bool(info.get("was_live")) or bool(info.get("is_live")))


def _track_url(pool: dict, prefer: list[str], code: str) -> str | None:
    """The best genuinely-native track in a subtitles/automatic_captions map, or None.

    Machine translations are rejected. YouTube offers every language as a translation of
    whatever was actually spoken, so an English video still advertises a "de" track — one
    whose URL carries tlang=de. Scoring that would measure Google Translate's vocabulary
    instead of the channel's, and would call an English channel German-subtitled.
    (Verified: a genuinely German video's own tracks never carry tlang.)
    """
    keys = dict.fromkeys([*prefer, *(k for k in pool if k.startswith(code))])
    for key in keys:
        fmts = [f for f in pool.get(key, []) if f.get("url") and "tlang=" not in f["url"]]
        if not fmts:
            continue
        best = (next((f for f in fmts if f.get("ext") == FMT), None)
                or next((f for f in fmts if f.get("ext") == "vtt"), None)
                or fmts[0])
        return best["url"]
    return None


def subtitle_track(info: dict, code: str) -> tuple[str, str] | None:
    """(source, url) for the best subtitle track in `code` and FMT, or None if none exists.

    source is "manual" or "auto"; manual is preferred, auto is the fallback. The "-orig" key
    is YouTube's own label for captions in the language actually spoken, so it ranks first.
    """
    url = _track_url(info.get("subtitles") or {}, [code, f"{code}-{code.upper()}"], code)
    if url:
        return "manual", url
    if MANUAL_ONLY:
        return None
    url = _track_url(info.get("automatic_captions") or {}, [f"{code}-orig", code], code)
    return ("auto", url) if url else None


# ------------------------------------------------------------- filesystem ---
def _sanitize(name: str) -> str:
    """A filesystem-safe folder/file name (keeps unicode, drops path-hostile chars)."""
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "", name).strip().strip(".")
    return re.sub(r"\s+", " ", name)[:120] or "unknown"


def target_path(folder: Path, info: dict, vid: str, source: str, code: str) -> Path:
    date = info.get("upload_date") or "00000000"
    suffix = f".{code}.{FMT}" if source == "manual" else f".{code}.auto.{FMT}"
    return folder / f"{date}_{vid}{suffix}"


def _fetch(url: str) -> str:
    r = httpx.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    if not r.text.strip():
        raise RuntimeError(f"empty subtitle body from {url[:80]}")
    return r.text


# ------------------------------------------------------------- video index ---
# One entry per video id, so a video is classified (Short? stream? which track?) exactly once.
# Without it, a cached .srt short-circuits the extract and we never learn whether the video was
# a stream — which is how stream VODs stayed silently mixed into the freshest-N window before.
# Bump whenever is_short / is_stream / subtitle_track change: entries classified by the old
# rules are wrong, and because a cached entry skips the extract they would otherwise never be
# revisited. This is deliberately blunt — it discards correct classification along with the
# stale, which costs a full re-extract, so change it only when the rules really moved.
INDEX_VERSION = 6


def index_path(code: str) -> Path:
    """Per language: a video belongs to one channel, and its track is language-specific."""
    return RUN_DIR / f"subs_index_{code}.json"


def load_index(code: str) -> dict:
    path = index_path(code)
    if not path.exists():
        return {}
    blob = json.loads(path.read_text(encoding="utf-8"))
    if blob.get("version") != INDEX_VERSION:
        print(f"subs: video index v{blob.get('version')} predates the current classification "
              f"rules (v{INDEX_VERSION}) — re-extracting", flush=True)
        return {}
    return blob["videos"]


def save_index(index: dict, code: str) -> None:
    path = index_path(code)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": INDEX_VERSION, "videos": index},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _classify(vid: str, title: str, folder: Path, code: str, duration: int = 0) -> dict:
    """Extract one video and save its subtitle track in `code`. Returns its index entry.

    `duration` is the Data API's answer, used when yt-dlp's response omits its own.
    """
    info = extract(f"https://www.youtube.com/watch?v={vid}")
    dur = info.get("duration") or duration
    entry = {
        "channel": title,
        "date": info.get("upload_date") or "00000000",
        "duration": dur,
        "aspect": aspect(info),
        "short": is_short(info, duration),
        "clip": 0 < dur < MIN_VIDEO_SEC,
        "stream": is_stream(info),
        "live_status": info.get("live_status"),
        "source": None,
        "file": None,
    }
    if entry["short"] or entry["clip"] or entry["stream"]:
        return entry
    track = subtitle_track(info, code)
    if track is None:
        return entry
    source, sub_url = track
    text = retry(lambda u=sub_url: _fetch(u))
    folder.mkdir(parents=True, exist_ok=True)
    path = target_path(folder, info, vid, source, code)
    path.write_text(text, encoding="utf-8")
    entry["source"] = source
    entry["file"] = str(path.relative_to(OUT_DIR))
    return entry


# --------------------------------------------------------------- per row ---
def cached_picks(row: dict, index: dict) -> list[dict] | None:
    """The sample an earlier run already took for this channel, or None if there isn't one.

    This is all of what `--reuse` buys: a channel already sampled costs *no network call at
    all*, not even the listing, so a rescore against a changed word list is instant for it.
    What it gives up is freshness — the sample stays whichever videos were newest on the day
    it was taken.

    Reconstructed from the video index rather than from the files on disk, because the index
    is the only record of a NORMAL video that turned out to have no subtitle track. Those
    videos produce no file, and dropping them would quietly flatter the Subs verdict: a
    channel whose three newest videos are [none, auto, auto] is "Auto" either way, but one
    whose newest three are [none, none, auto] is "No" and would read as "Auto" from the files
    alone.

    A sample whose subtitle file has since been deleted returns None — re-fetching is honest,
    scoring the remaining two videos as if they were the sample is not.
    """
    title = (row.get("_title") or "").strip()
    if not title:
        return None
    normal = [(vid, e) for vid, e in index.items()
              if (e.get("channel") or "") == title
              and not e["short"] and not e.get("clip") and not e["stream"]]
    if not normal:
        return None
    # Newest first, by upload date; the video id breaks a tie so the pick is deterministic.
    normal.sort(key=lambda ve: (ve[1].get("date") or "", ve[0]), reverse=True)
    picked = []
    for vid, entry in normal:
        if enough(picked):
            break
        path = OUT_DIR / entry["file"] if entry["file"] else None
        if path is not None and not path.exists():
            return None
        picked.append({"vid": vid, "date": entry["date"], "source": entry["source"],
                       "path": path, "duration": entry.get("duration") or 0})
    # A sample short of the target is always re-fetched, even when the index holds nothing
    # more: the index only knows the videos some earlier run bothered to classify, so
    # "no more entries" and "no more videos" are the same reading of two different facts, and
    # the first attempt at this guard reused three-video samples for the exact 18 channels it
    # was written to rescue. Telling them apart needs the channel listing, which is one cheap
    # call for the handful of channels this touches.
    return picked if enough(picked) else None


def _count_picked(picked: list[dict], stats: dict) -> None:
    """Fold a reused sample into the same counters the fetching path fills."""
    for video in picked:
        stats["normal"] += 1
        if video["source"] == "manual":
            stats["manual"] += 1
        elif video["source"] == "auto":
            stats["auto"] += 1
        else:
            stats["no_subs"] += 1


def collect(row: dict, index: dict, stats: dict, errors: list, code: str,
            reuse: bool = False) -> list[dict]:
    """The <=N_VIDEOS freshest normal videos of one channel, newest first.

    Each is {vid, date, source, path} where source is "manual" / "auto" / None. Downloads any
    subtitle track not already on disk and records every video it looks at in `index`.

    With `reuse`, a channel that already has a sample is returned from the index untouched —
    see cached_picks().
    """
    title = (row.get("_title") or "").strip() or (row.get("YT Channel ID") or "?")
    folder = OUT_DIR / _sanitize(title)
    cid = (row.get("YT Channel ID") or "").strip()
    if not cid:
        errors.append((title, "-", "no YT Channel ID"))
        return []

    if reuse:
        cached = cached_picks(row, index)
        if cached is not None:
            stats["reused"] += 1
            _count_picked(cached, stats)
            if len(cached) < N_VIDEOS:
                stats["short_channels"] += 1
            return cached

    picked: list[dict] = []
    listing = channel_videos(cid)
    vids = [v for v, _ in listing]
    secs = dict(listing)
    for vid in vids:
        if enough(picked):
            break
        entry = index.get(vid)
        if entry is None and 0 < secs.get(vid, 0) < MIN_VIDEO_SEC:
            # Too brief to be worth an extraction; the API duration alone settles it.
            entry = index[vid] = {"channel": title, "date": "", "duration": secs[vid],
                                  "aspect": None, "short": False, "clip": True,
                                  "stream": False, "live_status": None,
                                  "source": None, "file": None}
        if entry is None:
            try:
                entry = _classify(vid, title, folder, code, secs.get(vid, 0))
            except Exception as exc:  # noqa: BLE001 — record loudly, keep going
                global _blocked_streak
                errors.append((title, vid, traceback.format_exc()))
                if looks_blocked(str(exc)):
                    _blocked_streak += 1
                    if _blocked_streak >= BLOCK_STREAK:
                        wait_out_block(vid)
                        _blocked_streak = 0
                else:
                    _blocked_streak = 0
                continue
            _blocked_streak = 0
            index[vid] = entry
            stats["extracted"] += 1
        if entry["short"]:
            stats["shorts"] += 1
            continue
        if entry.get("clip"):
            stats["clips"] += 1
            continue
        if entry["stream"]:
            stats["streams"] += 1
            continue
        picked.append({"vid": vid, "date": entry["date"], "source": entry["source"],
                       "path": OUT_DIR / entry["file"] if entry["file"] else None,
                       "duration": entry.get("duration") or 0})
        stats["normal"] += 1
        if entry["source"] == "manual":
            stats["manual"] += 1
        elif entry["source"] == "auto":
            stats["auto"] += 1
        else:
            stats["no_subs"] += 1

    if len(picked) < N_VIDEOS:
        stats["short_channels"] += 1
    return picked


def sample_bytes(picked: list[dict]) -> int:
    """Raw subtitle bytes the sample carries. A video with no track contributes none."""
    return sum(v["path"].stat().st_size for v in picked
               if v.get("path") is not None and v["path"].exists())


def enough(picked: list[dict]) -> bool:
    """Is this sample done? N_VIDEOS is the floor, SAMPLE_BYTES the target, MAX_VIDEOS the cap."""
    if len(picked) >= MAX_VIDEOS:
        return True
    return len(picked) >= N_VIDEOS and sample_bytes(picked) >= SAMPLE_BYTES


def report_line(title: str, picked: list[dict]) -> str:
    marks = "".join({"manual": "M", "auto": "A", None: "-"}[v["source"]] for v in picked) or "-"
    return f"  {title}: videos={len(picked)}/{N_VIDEOS} [{marks}]"


def new_stats() -> dict:
    return dict.fromkeys(
        ("normal", "manual", "auto", "no_subs", "shorts", "clips", "streams", "extracted",
         "short_channels", "reused"), 0)


def resolve_lang(lang: str) -> tuple[str, str]:
    """("German", "de") for a Channels Lang value, checked against config.CL_LANGS."""
    if lang not in CL_LANGS:
        raise SystemExit(f"unknown language {lang!r}; config.CL_LANGS has "
                         f"{', '.join(sorted(CL_LANGS))}")
    return lang, CL_LANGS[lang]


def run(*, lang: str = CL_DEFAULT_LANG, limit: int | None = None,
        reuse: bool = False) -> tuple[dict, dict, dict, list]:
    """Fetch subtitles for every selected channel. Returns (per-channel picks, stats, index, errors).

    Per-channel picks are keyed by Notion page id so the caller can join them back to its rows.

    `reuse` returns an already-sampled channel straight from the index, with no network call —
    see cached_picks().
    """
    lang, code = resolve_lang(lang)
    rows = query_rows(CHANNELS_DB)
    channels = select_channels(rows, lang)
    # "or" rather than a get() default: the property exists on every row but is null on rows
    # whose Status was never set, and None does not sort against a string.
    channels.sort(key=lambda r: (r.get("Status") or "", (r.get("_title") or "").lower()))
    if limit:
        channels = channels[:limit]

    index = load_index(code)
    stats = new_stats()
    errors: list = []
    picks: dict[str, tuple[dict, list[dict]]] = {}
    for n, row in enumerate(channels, 1):
        picks[row["_id"]] = (row, collect(row, index, stats, errors, code, reuse))
        if n % 10 == 0:
            save_index(index, code)
            print(f"  ...{n}/{len(channels)} channels", flush=True)
    save_index(index, code)
    return picks, stats, index, errors


def format_stats(stats: dict, n_channels: int, n_errors: int) -> str:
    return (f"subs: channels={n_channels} normal={stats['normal']} "
            f"(manual={stats['manual']} auto={stats['auto']} no_subs={stats['no_subs']}) "
            f"shorts={stats['shorts']} clips={stats['clips']} streams={stats['streams']} "
            f"extracted={stats['extracted']} reused={stats['reused']} "
            f"errors={n_errors} -> {OUT_DIR.name}/")


# ------------------------------------------------------------------- main ---
def main() -> int:
    ap = argparse.ArgumentParser(description="Download subtitles for one language's YT channels.")
    ap.add_argument("--lang", default=CL_DEFAULT_LANG, choices=sorted(CL_LANGS),
                    help=f"Channels Lang value to process (default {CL_DEFAULT_LANG})")
    ap.add_argument("--limit", type=int, help="only the first N channels")
    ap.add_argument("--reuse", action="store_true",
                    help="keep the sample an earlier run took; fetch only unsampled channels")
    args = ap.parse_args()

    picks, stats, _index, errors = run(lang=args.lang, limit=args.limit, reuse=args.reuse)
    if not picks:
        print("subs: no channels match the view filter + status")
        return 0

    print(format_stats(stats, len(picks), len(errors)))
    for row, sel in picks.values():
        print(report_line((row.get("_title") or "?").strip(), sel))

    if errors:
        print(f"\n{len(errors)} ERROR(S):", file=sys.stderr)
        for title, vid, tb in errors:
            print(f"\n--- {title} [{vid}] ---\n{tb}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
