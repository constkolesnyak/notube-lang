"""Row pictures -> the Channels "Avatar" field (and the row's page icon).

Covers both kinds of Channels row: a YouTube channel gets its 800px channel avatar, a
Pocket Casts show gets its cover art (the "PC Cover" the podcasts stage already stores).

The field is a Notion files property with the image in it. The image is *imported* rather
than linked — Notion only
previews a linked file as an image when the URL path ends in .jpg/.png, and neither
source qualifies (a YouTube avatar URL ends in "=s800-c-k-c0x00ffffff-no-rj", a Pocket
Casts cover in .webp). Imported it renders as a thumbnail; linked it would be a file
chip. See notion.imported_file_prop. The page icon is a plain external link — icons
always render, whatever the extension.

The channel avatar costs no extra YouTube quota beyond the channels.list call itself —
the 800px image is already in the snippet the rest of the pipeline fetches and discards.
The podcast cover costs nothing at all: it comes off the row.

daily.py runs `run()` as a stage, which normally means the handful of rows the inbox and
podcasts stages just imported: a row that already has an Avatar is skipped, so a quiet
day writes nothing. Standalone it doubles as the backfill:

    uv run python -m notube.avatars                         # every row still missing one
    uv run python -m notube.avatars --rating 5 --limit 3    # a small sample to look at
    uv run python -m notube.avatars --dry-run               # print, write nothing
"""

from __future__ import annotations

import argparse
import sys
import traceback

import httpx

from notube import common  # noqa: F401  (importing forces IPv4 + socket timeout)
from notube.config import CHANNELS_DB
from notube.notion import call, client, imported_file_prop, query_rows, resolve_data_source_id
from notube.youtube import client as yt_client
from notube.youtube import fetch_channels, pick_thumbnail

FIELD = "Avatar"


def ensure_field(ds_id: str) -> None:
    """Create the Avatar files property if it does not exist yet.

    The guard is load-bearing: data_sources.update replaces whatever it touches, so a
    blind update of an existing property is never safe.
    """
    have = call(client().data_sources.retrieve, data_source_id=ds_id).get("properties", {})
    if FIELD in have:
        return
    call(client().data_sources.update, data_source_id=ds_id, properties={FIELD: {"files": {}}})
    print(f"avatars: created the {FIELD!r} field")


def fetchable(url: str) -> bool:
    """Whether Notion's importer will be able to read this image.

    Some shows keep a cover URL that 404s upstream. Notion answers the import with a
    plain ValidationError, which daily.py would surface as a hard error — every single
    day, since the row stays empty and comes back tomorrow. Checking first turns that
    into a quiet skip, the same as a channel with no avatar.
    """
    try:
        r = httpx.head(url, timeout=15.0, follow_redirects=True)
        if r.status_code == 405:  # HEAD not allowed — ask for the real thing
            r = httpx.get(url, timeout=15.0, follow_redirects=True)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def has_picture(row: dict) -> bool:
    """Whether this row is one we can picture at all: a YouTube channel or a podcast."""
    return bool((row.get("YT Channel ID") or "").strip() and not row.get("Type")
                or (row.get("PC Cover") or "").strip())


def select_rows(rows: list[dict], *, rating: str | None, limit: int | None,
                force: bool) -> list[dict]:
    """Rows to fill, in a stable order so a rerun picks the same ones.

    A channel row is one with a YT Channel ID and no Type (same discriminator the Notion
    views and subs.select_channels use); a podcast row is one carrying a PC Cover. Rows
    that already have an Avatar are left alone unless --force.
    """
    picked = [r for r in rows
              if has_picture(r)
              and (rating is None or r.get("Rating") == rating)
              and (force or not r.get(FIELD))]
    picked.sort(key=lambda r: (r.get("_title") or "").lower())
    return picked[:limit] if limit else picked


def write_row(row: dict, url: str) -> None:
    """Set Avatar (imported) and the page icon (linked) to the same image."""
    # The extension is what makes Notion preview the imported file as an image, so it
    # has to match the bytes: YouTube serves JPEG, Pocket Casts WebP.
    ext = "webp" if url.rsplit("?", 1)[0].endswith(".webp") else "jpg"
    name = f"{(row.get('_title') or 'row').strip()[:80]}.{ext}"
    call(client().pages.update, page_id=row["_id"],
         properties={FIELD: imported_file_prop(url, name)},
         icon={"type": "external", "external": {"url": url}})


def run(yt=None, *, rating: str | None = None, limit: int | None = None,
        force: bool = False, dry_run: bool = False, verbose: bool = False) -> dict:
    """Fill Avatar for every channel row missing one. Returns a summary for the caller:

    {rows, filled, no_avatar: [title], failed, errors: [str]} — `errors` are per-row write
    failures, each already printed in full; the caller decides how loud they are.
    """
    rows = select_rows(query_rows(CHANNELS_DB), rating=rating, limit=limit, force=force)
    summary: dict = {"rows": len(rows), "filled": 0, "no_avatar": [], "failed": 0, "errors": []}
    if not rows:
        return summary
    if verbose:
        print(f"avatars: {len(rows)} rows", flush=True)

    cids = [c for r in rows if (c := (r.get("YT Channel ID") or "").strip())]
    channels = fetch_channels(yt or yt_client(), cids) if cids else {}
    if not dry_run:
        ensure_field(resolve_data_source_id(CHANNELS_DB))

    for row in rows:
        title = (row.get("_title") or "?").strip()
        cid = (row.get("YT Channel ID") or "").strip()
        if cid:
            ch = channels.get(cid)
            url = pick_thumbnail(ch["snippet"]) if ch else ""
        else:
            url = (row.get("PC Cover") or "").strip()  # podcast: the cover the PC sync stored
        if not url or not fetchable(url):
            # Unresolved (terminated / hidden) channel, or an image the source no longer
            # serves: leave whatever is there. Writing an empty files value would clear a
            # good avatar a later run had put in.
            summary["no_avatar"].append(title)
            continue
        if verbose:
            print(f"  {title}: {url}")
        if dry_run:
            continue
        try:
            write_row(row, url)
            summary["filled"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad row must not abort the rest
            summary["failed"] += 1
            summary["errors"].append(f"{title}: {type(exc).__name__}: {str(exc).strip()[:100]}")
            print(f"avatars: FAILED {title}", file=sys.stderr)
            traceback.print_exc()
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fill the Channels 'Avatar' field (and page icon) for YouTube channels.")
    ap.add_argument("--rating", help="only rows with this Rating (e.g. 5)")
    ap.add_argument("--limit", type=int, help="only the first N channels (alphabetical)")
    ap.add_argument("--force", action="store_true", help="also redo rows that already have one")
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = ap.parse_args()

    s = run(rating=args.rating, limit=args.limit, force=args.force, dry_run=args.dry_run,
            verbose=True)
    if not s["rows"]:
        print("avatars: no channel rows to fill")
        return 0
    print(f"avatars: filled={s['filled']} no-avatar={len(s['no_avatar'])} failed={s['failed']}"
          + (" (dry run)" if args.dry_run else ""))
    if s["no_avatar"]:
        print("  no avatar: " + ", ".join(s["no_avatar"][:10])
              + (f" (+{len(s['no_avatar']) - 10} more)" if len(s["no_avatar"]) > 10 else ""))
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
