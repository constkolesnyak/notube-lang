"""All videos from non-inbox playlists -> the "YT Videos Backup" table (one row per video).

Every playlist that is not an inbox playlist (config.INBOX) is mirrored here: each
unique video gets a row with its title, link, channel + cheap channel metadata,
duration, the playlist(s) it is in, and an Unavailable tag if it went private/deleted.
Each run reconciles: new videos added, videos gone from all playlists deleted (moved
to Notion trash), and duplicate entries within a playlist removed (keep the first).

Functions return data; printing is the caller's job (daily.py).
"""

from __future__ import annotations

import traceback

from notube.config import BACKUP_WATCH_LATER, INBOX, VIDEOS_DB, WATCH_LATER_TITLE
from notube.innertube import get_innertube
from notube.notion import (
    call,
    client,
    prune_multi_select_options,
    query_rows,
    resolve_data_source_id,
    rich,
)
from notube.youtube import (
    batch_get_video_durations,
    batch_get_video_meta,
    build_values,
    channel_link,
    fetch_channels,
    list_my_playlists,
    list_playlist_items,
    pick_thumbnail,
    playlist_link,
    set_video_id,
    unavail_reason,
    video_link,
)
from notube.youtube import (
    client as yt_client,
)

INBOX_TITLES = {row[0] for row in INBOX}


# ----------------------------------------------------------------- collect ---
def collect(yt, it=None, playlists: list[dict] | None = None):
    """{videoId: rec} over all non-inbox playlists (one record per video). Also finds
    duplicate entries within a playlist (keep first, the rest -> dup_removals[pid]) and,
    for unavailable videos, where they sit so we can remove them (unavail_at[vid] =
    [(pid, setVideoId), ...]). When `it` (InnerTube) is given and BACKUP_WATCH_LATER is
    on, Watch Later is read via InnerTube and merged in as one more playlist (the Data
    API cannot see WL). Returns (by_vid, dup_removals, dup_report, unavail_at, wl_error);
    wl_error is a detailed message if the WL read failed (the Data API backup is kept)."""
    by_vid: dict[str, dict] = {}
    dup_removals: dict[str, list[str]] = {}
    dup_report: list[tuple[str, str]] = []  # (videoId, title)
    unavail_at: dict[str, list[tuple[str, str]]] = {}
    for pl in (playlists if playlists is not None else list_my_playlists(yt)):
        title = pl["snippet"]["title"]
        if title in INBOX_TITLES:
            continue
        pid = pl["id"]
        plink = playlist_link(pid)
        seen_pl: set[str] = set()
        for item in list_playlist_items(yt, pid):
            sn, cd, st = item.get("snippet", {}), item.get("contentDetails", {}), item.get("status", {})
            vid = cd.get("videoId") or sn.get("resourceId", {}).get("videoId")
            if not vid:
                continue
            if vid in seen_pl:  # duplicate within this playlist -> remove (keep the first)
                svid = set_video_id(item.get("id", ""))
                if svid:
                    dup_removals.setdefault(pid, []).append(svid)
                    dup_report.append((vid, sn.get("title", "")))
                continue
            seen_pl.add(vid)
            if unavail_reason(sn.get("title", ""), st.get("privacyStatus", "")):
                unavail_at.setdefault(vid, []).append((pid, set_video_id(item.get("id", ""))))
            rec = by_vid.get(vid)
            if rec is None:
                rec = by_vid[vid] = {
                    "video_id": vid, "title": sn.get("title", ""),
                    "cid": sn.get("videoOwnerChannelId", ""), "cname": sn.get("videoOwnerChannelTitle", ""),
                    "privacy": st.get("privacyStatus", ""), "published": cd.get("videoPublishedAt", ""),
                    "thumb": pick_thumbnail(sn), "playlists": [],
                }
            if (title, plink) not in rec["playlists"]:
                rec["playlists"].append((title, plink))

    wl_error = _merge_watch_later(yt, it, by_vid, dup_removals, dup_report, unavail_at)
    return by_vid, dup_removals, dup_report, unavail_at, wl_error


def _wl_record(w: dict, meta: dict | None) -> dict:
    """Build a collect rec for a WL item. The Data API `meta` (from videos.list) is
    authoritative and always present for a live video, so it fills channel + published +
    title; InnerTube reports dead WL entries as playable, so a *missing* meta (the API
    won't return deleted/private/blocked ids) is what marks a video unavailable, and the
    InnerTube placeholder only supplies the private-vs-deleted nuance. Fields are
    normalized so youtube.unavail_reason(title, privacy) classifies it like any row."""
    if meta and meta["privacy"] in ("public", "unlisted"):
        title, privacy = meta["title"] or w["title"], "public"
    elif meta and meta["privacy"] == "private":
        title, privacy = meta["title"] or w["title"] or "Private video", "private"
    else:  # absent from the API -> unavailable; InnerTube gives the nuance
        reason = w["placeholder"] or "unavailable"
        meta = None
        title = "Deleted video" if reason == "deleted" else (w["title"] or "Unavailable video")
        privacy = "private" if reason == "private" else ""
        if privacy == "private":
            title = w["title"] or "Private video"
    return {
        "video_id": w["video_id"], "title": title,
        "cid": (meta or {}).get("cid", ""), "cname": (meta or {}).get("cname", ""),
        "privacy": privacy, "published": (meta or {}).get("published", ""),
        "thumb": (meta or {}).get("thumb") or w["thumb"], "playlists": [],
    }


def _merge_watch_later(yt, it, by_vid, dup_removals, dup_report, unavail_at) -> str | None:
    """Read Watch Later via InnerTube and merge it into the collect structures as one
    more playlist (WATCH_LATER_TITLE). No-op if disabled or `it` is None. Read the whole
    list first (and its authoritative metadata/availability from the Data API), then
    merge: a mid-read failure discards the partial WL view (so we never prune from an
    incomplete read) while the Data API backup already collected is kept. On failure the
    full traceback is printed and returned — never swallowed silently.

    An *empty* read is second-guessed before it is believed. It is the one answer that
    cannot be told apart from "you emptied Watch Later", and believing a wrong one deletes
    every row whose only playlist was WL: measured 2026-08-29, a run read WL as empty and
    trashed 114 rows, and a fresh session 15 minutes later read the same 78 videos back.
    The session bucketing behind it (see innertube._reseed_from_chrome) is fixed at
    construction, so the second opinion has to come from a second session — re-asking this
    one would only repeat the answer. Zero twice is believed and deletes as before."""
    if not BACKUP_WATCH_LATER or it is None:
        return None
    wl_pid = "WL"
    wl_plink = playlist_link(wl_pid)
    try:
        staged = list(it.list_playlist_full(wl_pid))
        if not staged:
            print("  [videos] Watch Later read as empty — asking a second session", flush=True)
            it.reseed()
            staged = list(it.list_playlist_full(wl_pid))
            print(f"  [videos] second opinion: {len(staged)} items", flush=True)
        meta = batch_get_video_meta(yt, [w["video_id"] for w in staged if w["video_id"]])
    except Exception as exc:  # noqa: BLE001 — surfaced loudly (full traceback + report)
        traceback.print_exc()
        return f"Watch Later read failed: {type(exc).__name__}: {exc}"

    seen: set[str] = set()
    for w in staged:
        vid = w["video_id"]
        if not vid:
            continue
        if vid in seen:  # duplicate within WL -> remove (keep the first)
            if w["set_video_id"]:
                dup_removals.setdefault(wl_pid, []).append(w["set_video_id"])
                dup_report.append((vid, w["title"]))
            continue
        seen.add(vid)
        rec = by_vid.get(vid)
        if rec is None:
            rec = by_vid[vid] = _wl_record(w, meta.get(vid))
        if unavail_reason(rec["title"], rec["privacy"]):
            unavail_at.setdefault(vid, []).append((wl_pid, w["set_video_id"]))
        if (WATCH_LATER_TITLE, wl_plink) not in rec["playlists"]:
            rec["playlists"].append((WATCH_LATER_TITLE, wl_plink))
    return None


# ------------------------------------------------------------------ props ---
def _date(iso: str):
    return {"date": {"start": iso}} if iso else {"date": None}


def base_props(rec: dict, reason: str | None) -> dict:
    """Fields refreshed every run (cheap): the playlists and the unavailable tag."""
    names = [n.replace(",", " ")[:100] for n, _ in rec["playlists"]]  # multi_select forbids commas
    links = "\n".join(f"{n} - {l}" for n, l in rec["playlists"])
    return {
        "Playlists": {"multi_select": [{"name": n} for n in names]},
        "Playlist Links": rich(links),
        "Unavailable": {"select": {"name": reason}} if reason else {"select": None},
    }


def full_props(rec: dict, reason: str | None, duration: str, values: dict) -> dict:
    """Full property set for a NEW row (metadata is written once, at creation)."""
    props = base_props(rec, reason)
    props.update({
        "Name": {"title": [{"type": "text", "text": {"content": (rec["title"] or rec["video_id"])[:2000]}}]},
        "Link": {"url": video_link(rec["video_id"])},
        "Video ID": rich(rec["video_id"]),
        "Channel": rich(rec["cname"]),
        "Channel Link": {"url": channel_link(rec["cid"]) or None},
        "YT Channel ID": rich(rec["cid"] or values.get("YT Channel ID", "")),
        "YT Keywords": rich(values.get("YT Keywords", "")),
        "YT Uploads": {"url": values.get("YT Uploads") or None},
        "YT Description": rich(values.get("YT Description", "")),
        "Duration": rich(duration),
        "Published": _date(rec["published"]),
        "Thumbnail": {"url": rec["thumb"] or None},
    })
    return props


# --------------------------------------------------------- option cleanup ---
def prune_playlist_options(ds_id: str, *, dry_run: bool = False) -> list[str]:
    """Drop "Playlists" multi_select options that no row uses any more (a playlist that
    became empty or was deleted). Returns the removed names."""
    return prune_multi_select_options(VIDEOS_DB, ds_id, "Playlists", dry_run=dry_run)


# ------------------------------------------------------------------- run ----
def run(yt=None, *, dry_run: bool = False, limit: int | None = None,
        playlists: list[dict] | None = None) -> dict:
    """Sync the table. Returns a summary for the report."""
    yt = yt or yt_client()
    ds_id = resolve_data_source_id(VIDEOS_DB)

    # InnerTube is needed to READ Watch Later (the Data API can't) and to prune dead /
    # duplicate entries afterwards. Acquire once (cached singleton). A failure here is
    # reported loudly (wl_error, full traceback) but does not abort the Data API backup.
    it = None
    wl_error = None
    if BACKUP_WATCH_LATER:
        try:
            it = get_innertube()
        except Exception as exc:  # noqa: BLE001 — surfaced via wl_error, never swallowed
            traceback.print_exc()
            wl_error = f"Watch Later / InnerTube session failed: {type(exc).__name__}: {exc}"

    by_vid, dup_removals, dup_report, unavail_at, wl_read_error = collect(yt, it, playlists)
    wl_error = wl_error or wl_read_error
    existing = {r.get("Video ID"): r for r in query_rows(VIDEOS_DB) if r.get("Video ID")}

    # Same rule as the delete guard below: an unread Watch Later must not strip the
    # "Watch Later" label off rows that already carry it. Carry the label over from the
    # row's own last-known state, so a failed WL read leaves the table exactly as it was.
    if wl_error:
        wl_entry = (WATCH_LATER_TITLE, playlist_link("WL"))
        for vid, rec in by_vid.items():
            row = existing.get(vid)
            if (row and WATCH_LATER_TITLE in (row.get("Playlists") or [])
                    and wl_entry not in rec["playlists"]):
                rec["playlists"].append(wl_entry)

    counts = {"added": 0, "updated": 0, "deleted": 0}
    failed = 0                 # individual rows that failed to write (do not abort the run)
    fail_reasons: list[str] = []  # sample of why writes failed, so the report can name a cause
    unavailable: list = []     # all currently unavailable: (title, link, reason)
    newly_unavail: list = []   # became unavailable this run

    # An unavailable video is "backed up" once a table row exists for it (the row is the backup;
    # if it died while we had its metadata, that metadata is preserved — the archive update never
    # overwrites it). Backed-up dead entries are removed from the playlists and the row is kept as
    # a permanent archive. Ones with no row yet are left in place (a stub row is created below, and
    # they are removed on the next run).
    removals: dict[str, list[str]] = {pid: list(s) for pid, s in dup_removals.items()}  # dedup
    to_archive: list[tuple[str, str]] = []
    for vid, occ in unavail_at.items():
        row = existing.get(vid)
        if row is None:
            continue
        to_archive.append((vid, unavail_reason(by_vid[vid]["title"], by_vid[vid]["privacy"])))
        for pid, svid in occ:
            if svid:
                removals.setdefault(pid, []).append(svid)

    # Archive dead videos in Notion FIRST — set ONLY the Unavailable tag — and only THEN
    # remove them from the playlist. Order matters: the backup is written before the
    # source entry is deleted. Everything else (Playlists, Playlist Links, all metadata)
    # is left exactly as it was, so the last-known playlist of a now-dead video is kept.
    archived = 0
    for vid, reason in to_archive:
        row = existing[vid]
        if not dry_run:
            try:
                call(client().pages.update, page_id=row["_id"],
                     properties={"Unavailable": {"select": {"name": reason}}})
            except Exception:  # noqa: BLE001
                failed += 1
                continue
        archived += 1
        by_vid.pop(vid, None)      # handled here -> exclude from the normal reconcile
        existing.pop(vid, None)

    # Then prune dead + duplicate entries from the playlists via InnerTube (no quota).
    # Non-fatal: if cookies are unavailable the rows are already archived above; surfaced.
    dups_removed = sum(len(v) for v in dup_removals.values())
    dedup_error = None
    if removals and not dry_run:
        try:
            if it is None:
                it = get_innertube()
            for pid, svids in removals.items():
                it.remove_videos(pid, svids)
        except Exception as exc:  # noqa: BLE001 — cookies/InnerTube; surfaced via dedup_error
            traceback.print_exc()
            dedup_error = f"Playlist prune (dead / duplicate removal) failed: {type(exc).__name__}: {exc}"
            dups_removed = 0

    if limit is not None:
        by_vid = dict(list(by_vid.items())[:limit])

    new_vids = [v for v in by_vid if v not in existing]

    # Fetch metadata only for new videos (incremental and fast).
    durations = batch_get_video_durations(yt, new_vids) if new_vids and not dry_run else {}
    new_cids = sorted({by_vid[v]["cid"] for v in new_vids if by_vid[v]["cid"]})
    channels = fetch_channels(yt, new_cids) if new_cids and not dry_run else {}
    values_by_cid = {cid: build_values(ch, []) for cid, ch in channels.items()}

    def write(action: str, fn, **kw) -> None:
        nonlocal failed
        if dry_run:
            counts[action] += 1
            return
        try:
            call(fn, **kw)
            counts[action] += 1
        except Exception as exc:  # noqa: BLE001 — one bad row must not abort the sync
            failed += 1
            # Capture the reason so the report can name a real cause instead of a
            # bare count (which invites downstream guesswork about what broke).
            fail_reasons.append(f"{type(exc).__name__}: {exc}"[:200])

    for vid, rec in by_vid.items():
        reason = unavail_reason(rec["title"], rec["privacy"])
        if reason:
            unavailable.append((rec["title"] or vid, video_link(vid), reason))
        row = existing.get(vid)
        if row is None:
            write("added", client().pages.create,
                  parent={"type": "data_source_id", "data_source_id": ds_id},
                  properties=full_props(rec, reason, durations.get(vid, ""), values_by_cid.get(rec["cid"], {})))
            continue
        cur_pl = set(row.get("Playlists") or [])
        want_pl = {n.replace(",", " ")[:100] for n, _ in rec["playlists"]}
        cur_un = row.get("Unavailable")
        if cur_pl == want_pl and (cur_un or None) == reason:
            continue  # nothing changed
        if reason and not cur_un:
            newly_unavail.append((row.get("_title") or rec["title"] or vid, video_link(vid), reason))
        write("updated", client().pages.update, page_id=row["_id"], properties=base_props(rec, reason))

    # Delete rows for videos gone from every playlist (moved to Notion trash). With --limit
    # by_vid is partial, so skip. Unavailable rows are kept as backups, never auto-deleted.
    #
    # Skipped entirely when the Watch Later read failed: WL is one of the playlists a video
    # can live in, so an unread WL makes every WL-only video look "gone from every playlist"
    # and this loop would trash its backup row over a stale cookie. Seen for real on
    # 2026-07-31, when an expired YouTube session in the container turned a routine
    # run into "videos: 30 removed". A failed source read must never delete a backup.
    if limit is None and not wl_error:
        for vid, row in existing.items():
            if vid not in by_vid and not row.get("Unavailable"):
                write("deleted", client().pages.update, page_id=row["_id"], in_trash=True)

    # Every run: drop "Playlists" options that no row uses any more (empty/deleted
    # playlists). Runs after the reconcile so usage reflects the final table. Skipped
    # under --limit (the table view is partial). Loud on failure — never silent.
    playlists_pruned: list[str] = []
    option_error = None
    if limit is None:
        try:
            playlists_pruned = prune_playlist_options(ds_id, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 — surfaced via option_error
            traceback.print_exc()
            option_error = f"Playlist-option cleanup failed: {type(exc).__name__}: {exc}"

    return {"counts": counts, "failed": failed, "fail_reasons": fail_reasons,
            "archived": archived, "dups_removed": dups_removed,
            "dedup_error": dedup_error, "wl_error": wl_error, "option_error": option_error,
            "playlists_pruned": playlists_pruned,
            "unavailable": unavailable, "newly_unavail": newly_unavail}
