"""The daily sync. One command:

    uv run python -m notube.daily

It (1) imports every inbox playlist's channels into Channels, verifies nothing was
lost, and empties the playlist; (2) classifies the language of new channels and
gives every channel row still missing one its avatar picture;
(3) syncs the YT Videos Backup table (add new, delete gone, tag unavailable) and
removes duplicate playlist entries; and (4) syncs Pocket Casts subscriptions into
the same Channels table. Each stage is isolated: one failure never aborts the rest. At
the end it prints a short report and exits 0 (clean) or 1 (something needs attention).

Meant to run unattended. Read the report's RESULT line and the exit code.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import os
import sys
import traceback

from dotenv import load_dotenv

from notube import (
    avatars,
    channels_sync,
    common,  # noqa: F401  (forces IPv4 + socket timeout)
    lang,
    pocketcasts_sync,
    videos_sync,
    yt_session_refresh,
)
from notube.config import (
    CHANNELS_DB,
    INBOX,
    PODCASTS_RATING,
    PODCASTS_TAG,
)
from notube.notion import resolve_data_source_id
from notube.youtube import client as yt_client
from notube.youtube import list_my_playlists

# Substring of an error message -> what the operator (or model) should do about it.
HINTS = [
    ("notion unavailable", "Notion is down or rate-limited; retry later"),
    ("sapisid", "log into youtube.com in Chrome (emptying/dedup skipped)"),
    ("logged_in", "log into youtube.com in Chrome (emptying/dedup skipped)"),
    ("chrome cookie", "allow Keychain access for Chrome (emptying/dedup skipped)"),
    ("not logged into youtube", "the YouTube cookie jar died — quit Chrome, then re-export the "
                               "cookies of the logged-in profile into NOTUBE_COOKIES_FILE"),
    # httpx renders YouTube write rejections as "Client error '403 Forbidden' for url
    # 'https://www.youtube.com/youtubei/...'" — reads passing while writes 403 means the
    # rotating __Secure-*PSIDTS went stale (or the IP got flagged), NOT a dead jar.
    ("403 forbidden' for url 'https://www.youtube.com/youtubei",
     "YouTube rejected the write (403 on youtubei) — cookie rotation lapsed or the IP is "
     "flagged; the jar is NOT necessarily dead (reads worked). If it repeats after a fresh "
     "cookie export, the IP is the likelier cause"),
    ("timed out", "network or Keychain timeout; retry"),
    ("youtube auth", "re-auth YouTube: set GOOGLE_* in .env"),
    ("refresherror", "YouTube token expired; re-auth"),
    ("notion_api_key", "set NOTION_API_KEY in .env"),
    ("pocketcasts", "set POCKETCASTS_EMAIL/POCKETCASTS_PASSWORD in .env"),
    ("playwright", "playwright missing from the venv (it is a declared dep) — run `uv sync`"),
    ("/claude", "claude CLI not found on PATH — set NOTUBE_CLAUDE_BIN in .env or install to ~/.local/bin"),
    ("lang: ", "classifier failed/off-set: check the [lang] stderr lines; claude CLI reachable?"),
]


def _hint(text: str) -> str:
    low = text.lower()
    for key, msg in HINTS:
        if key in low:
            return msg
    return ""


def _err_line(stage: str, exc: Exception) -> str:
    msg = str(exc).strip().replace("\n", " ")[:160]
    hint = _hint(f"{type(exc).__name__} {msg}")
    line = f"  {stage}: {type(exc).__name__} {msg}"
    return line + (f" -> {hint}" if hint else "")


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily YouTube -> Notion sync (one command).")
    ap.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = ap.parse_args()
    dry = args.dry_run
    load_dotenv()  # so os.getenv sees POCKETCASTS_* below (other modules load it too)

    errors: list[str] = []       # hard failures (exit 1)
    warnings: list[str] = []     # soft notes (do not change exit code)
    today = datetime.date.today().isoformat()

    # --- concurrency guard: never let two runs race on the inbox ---
    # The inbox stage imports each playlist's channels into Channels and then EMPTIES
    # the playlist. If a second daily.py starts while the first is mid-flight, it
    # reaches the inbox after the first has already emptied it, finds nothing, and
    # prints "RESULT: ok (nothing to do)" — silently masking the real sync the first
    # run performed. That happened for real once: a sync with inbox +1 / videos +4 was
    # reported as "nothing to do" after the scheduler re-launched daily.py because the
    # long first run looked stuck.
    # Take a non-blocking exclusive lock; a second overlapping run refuses loudly with
    # a distinctive `busy` state (exit 2) that can never be mistaken for a clean run.
    _lock_fd = open("/tmp/notube-daily.lock", "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"\nDAILY SYNC {today}\n"
              "another daily.py run is already in progress — refusing to double-run\n"
              "RESULT: busy (another run in progress)  exit=2", flush=True)
        return 2
    globals()["_DAILY_LOCK_FD"] = _lock_fd  # keep the fd (and lock) alive for the whole process

    # --- startup: credentials + Channels data source ---
    try:
        yt = yt_client()
        ds_id = resolve_data_source_id(CHANNELS_DB)
        # One playlists.list walk for the whole run (inbox lookups + videos stage
        # used to re-walk it 7 times).
        playlists = list(list_my_playlists(yt))
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        print(f"\nDAILY SYNC {today}\nstartup failed\nerrors (1):\n{_err_line('startup', exc)}\n"
              f"RESULT: attention  exit=1")
        return 1

    # --- youtube session: keep the profile's cookies fresh ---
    # innertube reads YouTube cookies live out of the Chromium profile. Google rotates
    # the session cookies every few hours: a profile that keeps browsing gets the fresh
    # ones and survives for months, a profile left alone goes stale. So warm it here,
    # before anything reads it. Skipped in NOTUBE_COOKIES_FILE mode: there the jar
    # sustains itself — innertube persists the rotated cookies back to the file after
    # every run (save_cookie_jar), and yt_keepalive.py (from cron) tops it up between
    # runs, so no browser is involved at all.
    if os.getenv("NOTUBE_CHROME_PROFILE") and not os.getenv("NOTUBE_COOKIES_FILE"):
        print("[youtube] warming session", flush=True)
        try:
            if not yt_session_refresh.warm():
                warnings.append("  youtube session: the Chromium profile is not logged into "
                                "YouTube — log into youtube.com in that profile once")
        except Exception as exc:  # noqa: BLE001 — a cold profile must not abort the syncs
            traceback.print_exc()
            warnings.append(_err_line("youtube session", exc))

    # --- stage 1: inbox -> Channels ---
    print(f"[inbox] {len(INBOX)} playlists", flush=True)
    inbox_new = inbox_upd = inbox_emptied = not_emptied = write_failed = 0
    for title, tag, rating, status in INBOX:
        try:
            s = channels_sync.process_inbox(ds_id, title, tag, rating, yt=yt,
                                         status=status, dry_run=dry, reset=True,
                                         playlists=playlists)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            errors.append(_err_line("inbox import", exc))
            continue
        inbox_new += s["new"]
        inbox_upd += s["upd"]
        inbox_emptied += s["removed"]
        write_failed += s["failed"]
        if s["links_failed"]:
            warnings.append(f"  links scrape: {s['links_failed']} channels failed "
                            f"(see the [inbox] lines on stdout)")
        if not s["ok"] and not dry:  # in dry-run nothing is written, so verify always "fails"
            errors.append(f"  inbox verify: a playlist lost videos ({s['present']}/{s['total']})")
        if not s["cleared"]:
            not_emptied += 1
            if s["clear_error"]:
                warnings.append(_err_line("not emptied", RuntimeError(s["clear_error"])))

    # --- stage 1b: pocket casts subscriptions -> Channels ---
    pod = None
    if os.getenv("POCKETCASTS_EMAIL") and os.getenv("POCKETCASTS_PASSWORD"):
        print("[podcasts] subscriptions", flush=True)
        try:
            pod = pocketcasts_sync.sync(ds_id, tag=PODCASTS_TAG, rating=PODCASTS_RATING, dry_run=dry)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            errors.append(_err_line("podcasts", exc))
    else:
        warnings.append("  podcasts: skipped (POCKETCASTS_EMAIL/POCKETCASTS_PASSWORD not set)")

    # --- stage 2: language of every channel + podcast with an empty Lang ---
    # Any lang problem is a hard ERROR (exit 1): a crashed stage, or rows left empty
    # because the classifier failed (CLI missing/timeout) or returned an off-set answer.
    lang_set = 0
    lang_skipped = 0
    if not dry:
        print("[lang] channels", flush=True)
        try:
            res = lang.run()
            lang_set += res["set"]
            lang_skipped += res.get("skipped", 0)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            errors.append(_err_line("lang", exc))
        if pod is not None:
            print("[lang] podcasts", flush=True)
            try:
                res = lang.run_podcasts()
                lang_set += res["set"]
                lang_skipped += res.get("skipped", 0)
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                errors.append(_err_line("lang podcasts", exc))
        if lang_skipped:
            errors.append(f"  lang: {lang_skipped} rows left empty (classifier failed or gave an "
                          f"off-set answer — check the [lang] lines on stdout)")

    # --- stage 2b: channel avatars ---
    # Only rows whose Avatar is still empty, so on a normal day this is the handful of
    # channels stage 1 just imported and the stage writes nothing otherwise. A channel
    # that has no avatar at all (terminated/hidden) is skipped, not blanked, and simply
    # gets re-checked tomorrow.
    print("[avatars] channels", flush=True)
    av = None
    try:
        av = avatars.run(yt, dry_run=dry)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        errors.append(_err_line("avatars", exc))

    # --- stage 3: videos backup table ---
    print("[videos] syncing", flush=True)
    v = None
    try:
        v = videos_sync.run(yt, dry_run=dry, playlists=playlists)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        errors.append(_err_line("videos", exc))

    # --- report ---
    lines = [f"\nDAILY SYNC {today}{'  (dry-run)' if dry else ''}"]

    # Every stage prints exactly ONE delta-only line, always, in a fixed order that maps
    # 1:1 onto the notification bullets (inbox, lang, avatars, videos, podcasts). A line
    # lists only the counters that actually moved; a quiet stage collapses to
    # "<stage>: no changes". Absence of any of the five lines = bug.
    def _delta(stage: str, bits: list[str], empty: str = "no changes") -> str:
        kept = [b for b in bits if b]
        return f"{stage}: {', '.join(kept) if kept else empty}"

    # 1) inbox — videos imported to Notion (the playlist-clearing count `inbox_emptied`
    #    still drives the "nothing to do" check below, but is intentionally NOT shown:
    #    it's plumbing, and new/updated already say what landed in Notion).
    lines.append(_delta("inbox", [
        f"+{inbox_new} new" if inbox_new else "",
        f"{inbox_upd} updated" if inbox_upd else "",
    ]))

    # 2) lang — channels/podcasts language-classified this run
    lines.append(_delta("lang", [f"{lang_set} classified" if lang_set else ""]))

    # 2b) avatars — channel pictures filled in on Channels rows that had none
    av_changed = False
    if av is None:
        lines.append("avatars: ERROR (see errors below)")
    else:
        av_changed = bool(av["filled"])
        lines.append(_delta("avatars", [f"+{av['filled']}" if av["filled"] else ""]))
        for e in av["errors"]:  # per-row write failures -> hard errors, with detail
            errors.append(f"  avatar row failed: {e}")

    # 3) videos — the videos backup table
    v_changed = False
    if v is None:
        lines.append("videos: ERROR (see errors below)")
    else:
        c = v["counts"]
        write_failed += v["failed"]
        pruned = v.get("playlists_pruned") or []
        v_changed = bool(c["added"] or c["deleted"] or c["updated"]
                         or v["dups_removed"] or v["archived"] or v["newly_unavail"] or pruned)
        lines.append(_delta("videos", [
            f"+{c['added']} new" if c["added"] else "",
            f"{c['deleted']} removed" if c["deleted"] else "",
            f"{c['updated']} updated" if c["updated"] else "",
            f"{v['dups_removed']} dups" if v["dups_removed"] else "",
            f"{v['archived']} archived" if v["archived"] else "",
            f"+{len(v['newly_unavail'])} unavailable" if v["newly_unavail"] else "",
            f"{len(pruned)} playlist-opts" if pruned else "",
        ]))
        # Watch Later read / playlist prune / option cleanup failures are hard errors
        # (exit 1), never silent: the full traceback is already on stdout; this adds a line.
        if v["dedup_error"]:
            errors.append(_err_line("prune", RuntimeError(v["dedup_error"])))
        if v["wl_error"]:
            errors.append(_err_line("watch-later", RuntimeError(v["wl_error"])))
        if v.get("option_error"):
            errors.append(_err_line("playlist-opts", RuntimeError(v["option_error"])))

    # 4) podcasts — Pocket Casts subscriptions
    pod_changed = False
    if pod is None:
        lines.append("podcasts: skipped (no credentials)")
    else:
        write_failed += pod["failed"]
        pod_changed = bool(pod["new"] or pod["upd"] or pod["dropped"])
        lines.append(_delta("podcasts", [
            f"+{pod['new']} new" if pod["new"] else "",
            f"{pod['upd']} updated" if pod["upd"] else "",
            f"{pod['dropped']} dropped" if pod["dropped"] else "",
        ]))

    if not_emptied:
        lines.append(f"not emptied: {not_emptied} playlists (youtube write failed — see warnings)")
    if write_failed:
        # Name the actual cause(s) so the report never bottoms out at a bare count.
        # A deduped sample keeps the line short while still telling you what broke —
        # e.g. a transient "HTTPResponseError: 502" (rerun clears it) vs. a real
        # "validation_error: ... is archived" (needs a fix, rerun won't help).
        reasons = (v or {}).get("fail_reasons") or []
        seen: list[str] = []
        for r in reasons:
            if r not in seen:
                seen.append(r)
        detail = f" — {'; '.join(seen[:3])}" if seen else ""
        errors.append(f"  writes: {write_failed} rows failed to save (rerun){detail}")
    if warnings:
        lines.append(f"warnings ({len(warnings)}):")
        lines.extend(warnings)
    if errors:
        lines.append(f"errors ({len(errors)}):")
        lines.extend(errors)

    nothing = not (inbox_new or inbox_upd or inbox_emptied or lang_set or av_changed
                   or v_changed or pod_changed)
    if errors or not_emptied:
        result, code = "attention", 1
    elif nothing:
        result, code = "ok (nothing to do)", 0
    else:
        result, code = "ok", 0
    lines.append(f"RESULT: {result}  exit={code}")
    print("\n".join(lines), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
