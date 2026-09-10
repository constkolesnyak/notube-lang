"""Inbox playlists -> Channels database (one row per channel), then empty the playlist.

For each channel that appears in an inbox playlist we create or update a Channels row
(Name, Link, Tags += <tag>, optional Rating, the "YT ..." metadata, and a "YT Saved"
link to that channel's videos from the playlist). Rows are matched by YT Channel ID
(also parsed from a channel/UC... Link) and then by normalised Link, so no duplicates.
A playlist carrying a status additionally writes it to the channel's Status (the I-*
queues say Seeing/Next/Soon/Later, Manual Inbox says Inbox); a playlist without one
clears Status on re-sync. After verifying every playlist video landed in Channels,
the playlist is emptied.

Functions return data; printing is the caller's job (daily.py).
"""

from __future__ import annotations

import random
import re
import time

from notube.config import CHANNELS_DB
from notube.innertube import get_innertube
from notube.notion import call, client, query_rows, rich
from notube.youtube import (
    build_values,
    channel_links,
    fetch_channels,
    list_featured_channels,
    list_my_playlists,
    list_playlist_items,
    set_video_id,
    transform_link,
    videos_url,
)
from notube.youtube import (
    client as yt_client,
)

WATCH_VIDEOS = "https://www.youtube.com/watch_videos?video_ids="
WATCH_CAP = 50  # the watch_videos trick shows at most ~50 videos
CHANNEL_IN_LINK = re.compile(r"channel/(UC[\w-]+)", re.I)
VIDEO_IDS_RE = re.compile(r"[?&]video_ids=([\w,\-]+)")
WATCH_V_RE = re.compile(r"[?&]v=([\w-]+)")
ID_LIKE = re.compile(r"^[A-Za-z0-9_-]{12,}$")  # looks like a playlist id (no spaces/emoji)


# ----------------------------------------------------------------- playlist ---
def find_playlist(yt, ident: str, playlists: list[dict] | None = None) -> tuple[str, str]:
    """(playlist_id, title) from a playlist id or title. Pass `playlists` (a cached
    list_my_playlists result) to avoid re-walking the account per lookup."""
    source = playlists if playlists is not None else list_my_playlists(yt)
    by_title = [(p["id"], p["snippet"].get("title", "")) for p in source]
    exact = [(pid, t) for pid, t in by_title if t == ident]
    if exact:
        return exact[0]
    if ID_LIKE.match(ident) and " " not in ident:
        meta = yt.playlists().list(part="snippet", id=ident, maxResults=1).execute(num_retries=5)
        if meta.get("items"):
            return ident, meta["items"][0]["snippet"].get("title", ident)
    titles = "\n".join(f"  - {t!r}" for _, t in by_title) or "  (no playlists)"
    raise RuntimeError(f"Playlist {ident!r} not found. Your playlists:\n{titles}")


def read_grouped(yt, playlist_id: str):
    """Group a playlist's videos by channel.

    Returns (order, vids_by_cid, titles_by_cid, skipped, items): channel ids in
    first-seen order, video ids per channel (deduped, in playlist order), channel
    titles, a count of items skipped because they had no owner (private/deleted),
    and every (videoId, playlistItem id) pair — clear_playlist() consumes the pairs
    so the playlist is read only once."""
    order: list[str] = []
    vids_by_cid: dict[str, list[str]] = {}
    titles_by_cid: dict[str, str] = {}
    skipped = 0
    items: list[tuple[str, str]] = []
    for it in list_playlist_items(yt, playlist_id):
        sn = it.get("snippet", {})
        vid = it.get("contentDetails", {}).get("videoId") or sn.get("resourceId", {}).get("videoId")
        cid = sn.get("videoOwnerChannelId")
        if vid:
            items.append((vid, it.get("id", "")))
        if not vid or not cid:
            skipped += 1
            continue
        if cid not in vids_by_cid:
            vids_by_cid[cid] = []
            titles_by_cid[cid] = sn.get("videoOwnerChannelTitle") or ""
            order.append(cid)
        if vid not in vids_by_cid[cid]:
            vids_by_cid[cid].append(vid)
    return order, vids_by_cid, titles_by_cid, skipped, items


# ----------------------------------------------------------------- YT Saved ---
def build_videos_url(video_ids: list[str]) -> str:
    ids = list(dict.fromkeys(v for v in video_ids if v))  # dedupe, keep order
    return WATCH_VIDEOS + ",".join(ids) if ids else ""


def parse_video_ids(url: str) -> list[str] | None:
    """Video ids from an existing YT Saved url. None if the format is unrecognised
    (the caller then leaves the field untouched)."""
    u = (url or "").strip()
    if not u:
        return []
    m = VIDEO_IDS_RE.search(u)
    if m:
        return [v for v in m.group(1).split(",") if v]
    m = WATCH_V_RE.search(u)
    if m:
        return [m.group(1)]
    return None


# ----------------------------------------------------------------- indexes ---
def build_indexes(rows: list[dict]) -> tuple[dict, dict]:
    """by_cid: UC... -> row (from YT Channel ID and from channel/UC... in Link);
    by_link: normalised Link -> row."""
    by_cid: dict[str, dict] = {}
    by_link: dict[str, dict] = {}
    for r in rows:
        link = r.get("Link") or ""
        m = CHANNEL_IN_LINK.search(link)
        if m:
            by_cid.setdefault(m.group(1), r)
        cid = (r.get("YT Channel ID") or "").strip()
        if cid:
            by_cid[cid] = r  # the field wins over the link
        norm = transform_link(link)
        if norm:
            by_link.setdefault(norm, r)
    return by_cid, by_link


def make_props(name: str, link: str, tags: list[str], values: dict, yt_links: str | None,
               yt_videos: str | None, rating: str | None, lang: str | None = None) -> dict:
    """Notion properties. Empty YT metadata is omitted, never blanking an existing value
    (e.g. an unresolved channel keeps the data from an earlier sync). yt_links/yt_videos=None
    -> leave that field as is; rating/lang=None too."""
    props = {
        "Name": {"title": [{"type": "text", "text": {"content": (name or "")[:2000]}}]},
        "Link": {"url": link or None},
        "Tags": {"multi_select": [{"name": t} for t in tags]},
    }
    # Only write metadata we actually have, so a missing field never clears a real value.
    for f in ("YT Channel ID", "YT Keywords", "YT Description", "YT Channels"):
        if (values.get(f) or "").strip():
            props[f] = rich(values[f])
    if values.get("YT Uploads"):
        props["YT Uploads"] = {"url": values["YT Uploads"]}
    if yt_links is not None:
        props["YT Links"] = rich(yt_links)
    if yt_videos is not None:
        props["YT Saved"] = {"url": yt_videos or None}
    if rating is not None:
        props["Rating"] = {"select": {"name": rating}}
    if lang is not None:
        props["Lang"] = {"select": {"name": lang}}
    return props


# ------------------------------------------------------------------ upsert ---
def upsert(yt, ds_id: str, order: list[str], vids_by_cid: dict[str, list[str]],
           titles_by_cid: dict[str, str], *, tag: str | None, rating: str | None,
           lang: str | None = None, status: str | None = None, dry_run: bool = False) -> dict:
    """Create/update a Channels row per channel. Returns counts + any anomalies.

    status names the queue each channel is put in (it must be an option on Channels' Status
    select); status=None clears an existing row's Status on re-sync instead. tag=None adds
    no tag (the playlist only routes to a queue)."""
    rows = query_rows(CHANNELS_DB)
    by_cid, by_link = build_indexes(rows)
    channels = fetch_channels(yt, order)

    counts = {"new": 0, "upd": 0, "unresolved": 0}
    failed = 0                         # rows that failed to write (do not abort)
    links_failed = 0                   # /about scrapes that errored (row still written)
    yt_videos_skipped: list[str] = []  # names whose existing YT Saved was unrecognised
    oversize: list[str] = []           # names with > WATCH_CAP video ids

    for cid in order:
        new_ids = vids_by_cid[cid]
        ch = channels.get(cid)
        if ch is not None:
            snip = ch["snippet"]
            name = snip.get("title", "") or titles_by_cid.get(cid, "")
            custom = snip.get("customUrl", "")
            feat = [] if dry_run else list_featured_channels(yt, cid)
            values = build_values(ch, feat)
        else:  # channel did not resolve (terminated/unavailable) — write what we have
            counts["unresolved"] += 1
            name = titles_by_cid.get(cid, "") or cid
            custom = ""
            values = {"YT Channel ID": cid}

        link = transform_link(videos_url(custom, cid))
        row = by_cid.get(cid) or by_link.get(link)

        yt_links = None  # None -> leave the existing YT Links untouched
        # The /about scrape is a full ~1MB page load — the most bot-flaggable
        # traffic of the whole run. Only scrape channels we know nothing about;
        # blank the YT Links field in Notion to force a re-scrape.
        if not dry_run and (row is None or not (row.get("YT Links") or "").strip()):
            try:
                scraped = "\n".join(f"{t} - {u}" if t else u for t, u in channel_links(cid))
                yt_links = scraped or None  # only write when the /about scrape returned links;
            except Exception as exc:  # noqa: BLE001 — a flaky scrape must never overwrite real links
                links_failed += 1
                print(f"  [inbox] links scrape failed for {cid}: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(random.uniform(1.0, 2.5))  # pace the page loads
        if row is None:
            counts["new"] += 1
            merged = new_ids
            tags = [tag] if tag else []
        else:
            counts["upd"] += 1
            existing = parse_video_ids(row.get("YT Saved") or "")
            tags = list(dict.fromkeys((row.get("Tags") or []) + ([tag] if tag else [])))
            if existing is None:  # unknown format — don't overwrite YT Saved
                merged = None
                yt_videos_skipped.append(name)
            else:
                merged = existing + [v for v in new_ids if v not in existing]

        yt_videos = None if merged is None else build_videos_url(merged)
        if merged is not None and len(merged) > WATCH_CAP:
            oversize.append(name)

        if dry_run:
            continue
        props = make_props(name, link, tags, values, yt_links, yt_videos, rating, lang)
        try:
            if row is None:
                if status:  # send a brand-new channel to its queue
                    props["Status"] = {"select": {"name": status}}
                call(client().pages.create,
                     parent={"type": "data_source_id", "data_source_id": ds_id}, properties=props)
            else:
                # a playlist with a status queues the channel; one without clears its Status
                props["Status"] = {"select": {"name": status}} if status else {"select": None}
                call(client().pages.update, page_id=row["_id"], properties=props)
        except Exception:  # noqa: BLE001 — one bad row must not abort the import
            failed += 1

    return {"counts": counts, "failed": failed, "links_failed": links_failed,
            "yt_videos_skipped": yt_videos_skipped, "oversize": oversize}


def verify_coverage(vids_by_cid: dict[str, list[str]], titles_by_cid: dict[str, str],
                    rows: list[dict]) -> dict:
    """Check every playlist video id is in its channel's YT Saved. Returns
    {total, present, lost:[(name,cid,vid)], norow:[(cid,name)]}."""
    by_cid, _ = build_indexes(rows)
    total = present = 0
    lost: list = []
    norow: list = []
    for cid, vids in vids_by_cid.items():
        r = by_cid.get(cid)
        if r is None:
            norow.append((cid, titles_by_cid.get(cid, "")))
        stored = set(parse_video_ids(r.get("YT Saved") or "") or []) if r else set()
        for v in vids:
            total += 1
            if v in stored:
                present += 1
            else:
                lost.append((titles_by_cid.get(cid, ""), cid, v))
    return {"total": total, "present": present, "lost": lost, "norow": norow}


def clear_playlist(it, pid: str, items: list[tuple[str, str]], verified: set[str]) -> dict:
    """Remove every playlist item whose video is in `verified` (imported + checked).
    `items` is read_grouped()'s (videoId, playlistItem id) list — reusing it saves a
    second full playlist walk; anything added to the playlist after that read is
    simply left in place. Returns {removed, left:[video_ids], undecoded}."""
    to_remove: list[str] = []
    left: list[str] = []
    undecoded = 0
    for vid, item_id in items:
        if vid in verified:
            svid = set_video_id(item_id)
            if svid:
                to_remove.append(svid)
            else:
                undecoded += 1
        else:
            left.append(vid)
    it.remove_videos(pid, to_remove)
    return {"removed": len(to_remove), "left": left, "undecoded": undecoded}


# -------------------------------------------------------------- one playlist --
def process_inbox(ds_id: str, title: str, tag: str | None, rating: str | None, *,
                  yt=None, lang: str | None = None, status: str | None = None,
                  dry_run: bool = False, reset: bool = False,
                  playlists: list[dict] | None = None) -> dict:
    """Full handling of one inbox playlist: import -> verify -> (optionally) empty.
    Never raises for an emptying failure (import already happened); returns a summary."""
    yt = yt or yt_client()
    pid, ptitle = find_playlist(yt, title, playlists)
    order, vids_by_cid, titles_by_cid, _, items = read_grouped(yt, pid)
    vids = {c: vids_by_cid[c] for c in order}

    res = upsert(yt, ds_id, order, vids, titles_by_cid, tag=tag, rating=rating, lang=lang,
                 status=status, dry_run=dry_run)
    rep = verify_coverage(vids, titles_by_cid, query_rows(CHANNELS_DB))
    ok = not rep["lost"] and rep["present"] == rep["total"]

    summary = {"title": ptitle, "channels": len(order),
               "new": res["counts"]["new"], "upd": res["counts"]["upd"], "failed": res["failed"],
               "links_failed": res["links_failed"],
               "present": rep["present"], "total": rep["total"], "ok": ok,
               "removed": 0, "left": 0, "cleared": True, "clear_error": None,
               "cids": list(order)}

    if not reset or dry_run or not order or not ok:
        return summary
    try:
        it = get_innertube()
        verified = {v for vs in vids.values() for v in vs}
        r = clear_playlist(it, pid, items, verified)
        summary.update(removed=r["removed"], left=len(r["left"]), cleared=r["undecoded"] == 0)
    except Exception as exc:  # noqa: BLE001 — cookies/InnerTube; import already done
        summary.update(cleared=False, clear_error=str(exc))
    return summary
