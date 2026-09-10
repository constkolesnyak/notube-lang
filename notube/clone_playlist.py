"""Clone a YouTube playlist into a new one, grouped by channel (no Data API quota).

Reads the source playlist with the Data API and writes the copy with InnerTube
(the logged-in Chrome session), so playlist writes cost no Data API quota.

    uv run python -m notube.clone_playlist --from <PLAYLIST_ID>
    uv run python -m notube.clone_playlist --from <ID> --sort channels --title "My copy" --privacy unlisted
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from notube.innertube import PRIVACY, InnerTube
from notube.youtube import client, list_playlist_items


def read_source(yt, playlist_id: str) -> tuple[str, list[tuple[str, str, str]]]:
    """(title, [(video_id, channel_id, channel_title), ...]) in source order."""
    meta = yt.playlists().list(part="snippet", id=playlist_id, maxResults=1).execute(num_retries=5)
    title = meta["items"][0]["snippet"]["title"] if meta.get("items") else playlist_id
    items: list[tuple[str, str, str]] = []
    for it in list_playlist_items(yt, playlist_id):
        sn = it.get("snippet", {})
        vid = it.get("contentDetails", {}).get("videoId") or sn.get("resourceId", {}).get("videoId")
        if vid:
            items.append((vid, sn.get("videoOwnerChannelId") or "(none)",
                          sn.get("videoOwnerChannelTitle") or "(none)"))
    return title, items


def order_videos(items: list[tuple[str, str, str]], sort: str) -> list[str]:
    """Video ids in the chosen order. Within a channel, source order is kept."""
    if sort == "none":
        return [v for v, _, _ in items]
    counts = Counter(c for _, c, _ in items)
    name: dict[str, str] = {}
    for _, c, t in items:
        name.setdefault(c, t)
    if sort == "channels":
        key = lambda c: (-counts[c], name[c].casefold())
    elif sort == "alpha":
        key = lambda c: name[c].casefold()
    elif sort == "appearance":
        first: dict[str, int] = {}
        for i, (_, c, _) in enumerate(items):
            first.setdefault(c, i)
        key = lambda c: first[c]
    else:
        raise ValueError(f"unknown --sort: {sort}")
    rank = {c: i for i, c in enumerate(sorted(counts, key=key))}
    idx = sorted(range(len(items)), key=lambda i: (rank[items[i][1]], i))
    return [items[i][0] for i in idx]


def main() -> int:
    ap = argparse.ArgumentParser(description="Clone a playlist grouped by channel (no Data API quota).")
    ap.add_argument("--from", dest="src", required=True, help="source playlist id")
    ap.add_argument("--sort", choices=["channels", "alpha", "appearance", "none"], default="channels")
    ap.add_argument("--title", default=None, help="new playlist title (default '<source> - by channel')")
    ap.add_argument("--privacy", choices=list(PRIVACY), default="private")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src_title, items = read_source(client(), args.src)
    vids = order_videos(items, args.sort)
    title = args.title or f"{src_title} - by channel"
    print(f"source: {src_title!r} | videos: {len(vids)} | sort={args.sort} | privacy={args.privacy}")
    print(f"new title: {title!r}")
    if args.dry_run:
        print("dry-run: nothing created")
        return 0

    it = InnerTube.from_chrome()
    pid = it.create_playlist(title, vids, privacy=PRIVACY[args.privacy])
    print(f"done: {len(vids)} videos -> https://www.youtube.com/playlist?list={pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
