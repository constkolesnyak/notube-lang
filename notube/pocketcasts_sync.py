"""Pocket Casts subscriptions -> Channels database (one row per show).

For every show the account is subscribed to we create or update a Channels row: Name,
Link (the canonical pocketcasts.com/podcasts/<uuid> page — the uuid in it is the
match key), Tags += <tag> and "PC", the "PC ..." metadata, an optional Rating on brand-new
rows, and Status = "Seeing". Rows are matched by uuid (the "PC ID" field, else parsed
from the Link), so re-running never duplicates. A podcast row whose show is no longer
in the subscription list is reconciled ONLY if its Status is currently "Seeing": its
Status is then set to INBOX_STATUS (the review queue); rows in any other state are left
alone. Existing data is merged losslessly: Tags are unioned and an edited Link is left as is.

Functions return data; printing is the caller's job (daily.py).
"""

from __future__ import annotations

import re

from notube import pocketcasts
from notube.config import CHANNELS_DB, INBOX_STATUS
from notube.notion import call, client, query_rows, rich

UUID_IN_LINK = re.compile(r"podcasts/([0-9a-f-]+)", re.I)

STATUS_SUBSCRIBED = "Seeing"   # Status for shows currently subscribed in Pocket Casts
SOURCE_TAG = "PC"              # marks the row as a podcast, next to the caller's own tag


def index_by_uuid(rows: list[dict]) -> dict[str, dict]:
    """uuid -> row, from the 'PC ID' field and from podcasts/<uuid> in the Link."""
    by_uuid: dict[str, dict] = {}
    for r in rows:
        m = UUID_IN_LINK.search(r.get("Link") or "")
        if m:
            by_uuid.setdefault(m.group(1).lower(), r)
        uuid = (r.get("PC ID") or "").strip().lower()
        if uuid:
            by_uuid[uuid] = r  # the field wins over the link
    return by_uuid


def make_props(name: str, tags: list[str], values: dict, *, link: str | None,
               rating: str | None) -> dict:
    """Notion properties. link/rating=None -> leave that field untouched (so an edited
    Link or a hand-set Rating is never clobbered). PC ID is always written (it is the
    match key); other "PC ..." fields only when non-empty, so they never blank a value."""
    props: dict = {
        "Name": {"title": [{"type": "text", "text": {"content": (name or "")[:2000]}}]},
        "Tags": {"multi_select": [{"name": t} for t in tags]},
        "PC ID": rich(values["PC ID"]),
    }
    if link is not None:
        props["Link"] = {"url": link or None}
    if rating is not None:
        props["Rating"] = {"select": {"name": rating}}
    for f in ("PC Author", "PC Description"):
        if values.get(f):
            props[f] = rich(values[f])
    for f in ("PC Feed", "PC Cover"):
        if values.get(f):
            props[f] = {"url": values[f]}
    return props


def sync(ds_id: str, *, tag: str, rating: str | None = None, dry_run: bool = False) -> dict:
    """Upsert a Channels row per subscribed show. An existing row is rewritten only when
    something actually changed (so a daily re-run is quiet, not 80 no-op writes).
    Returns counts; never raises for a single bad row (it is counted in 'failed')."""
    rows = query_rows(CHANNELS_DB)
    by_uuid = index_by_uuid(rows)
    subs = pocketcasts.list_subscriptions()
    sub_uuids = {u for s in subs if (u := (s.get("uuid") or "").strip().lower())}

    new = upd = unchanged = dropped = failed = 0

    # pass 1: currently-subscribed shows -> upsert, Status "Seeing"
    for show in subs:
        uuid = (show.get("uuid") or "").strip().lower()
        if not uuid:
            continue
        name = show.get("title") or uuid
        values = pocketcasts.build_values(show)
        row = by_uuid.get(uuid)

        if row is None:
            tags = [tag, SOURCE_TAG]
            link = pocketcasts.share_url(uuid)
            props = make_props(name, tags, values, link=link, rating=rating)  # default Rating: new rows only
            new += 1
        else:
            tags = list(dict.fromkeys((row.get("Tags") or []) + [tag, SOURCE_TAG]))  # union, lossless
            link = None if (row.get("Link") or "").strip() else pocketcasts.share_url(uuid)
            metadata_changed = any(
                values[f] and values[f] != (row.get(f) or "")
                for f in ("PC ID", "PC Author", "PC Description", "PC Feed", "PC Cover")
            )
            if not (tags != (row.get("Tags") or []) or name != (row.get("Name") or "")
                    or link is not None or metadata_changed
                    or row.get("Status") != STATUS_SUBSCRIBED):
                unchanged += 1
                continue
            props = make_props(name, tags, values, link=link, rating=None)  # never touch an existing Rating
            upd += 1
        props["Status"] = {"select": {"name": STATUS_SUBSCRIBED}}

        if dry_run:
            continue
        try:
            if row is None:
                call(client().pages.create,
                     parent={"type": "data_source_id", "data_source_id": ds_id}, properties=props)
            else:
                call(client().pages.update, page_id=row["_id"], properties=props)
        except Exception:  # noqa: BLE001 — one bad row must not abort the import
            failed += 1

    # pass 2: a row no longer in the subscription list is demoted ONLY if it is currently
    # "Seeing" (an active subscription) -> Status = INBOX_STATUS (the review queue). Rows in
    # any other state (Completed, already queued, hand-set) are left untouched, subscribed or not.
    for uuid, row in by_uuid.items():
        if uuid in sub_uuids or row.get("Status") != STATUS_SUBSCRIBED:
            continue
        dropped += 1
        if dry_run:
            continue
        props = {"Status": {"select": {"name": INBOX_STATUS}}}
        try:
            call(client().pages.update, page_id=row["_id"], properties=props)
        except Exception:  # noqa: BLE001
            failed += 1

    return {"new": new, "upd": upd, "unchanged": unchanged, "dropped": dropped,
            "failed": failed, "total": len(subs)}
