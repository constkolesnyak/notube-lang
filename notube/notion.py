"""Notion API helpers: client, schema-aware reads (with retry), and a write wrapper.

Notion API note (version 2025-09-03): a database's id is not the id you query rows
from — each database has a separate "data source" id. `resolve_data_source_id`
turns a database id/URL into that data source id.
"""

from __future__ import annotations

import os
import re
import time
from functools import lru_cache

from dotenv import load_dotenv
from notion_client import APIResponseError, Client
from notion_client.errors import HTTPResponseError, RequestTimeoutError

from notube import common  # noqa: F401  (importing forces IPv4 + socket timeout)

_HEX32 = re.compile(r"[0-9a-f]{32}")


@lru_cache(maxsize=1)
def client() -> Client:
    """Notion client from NOTION_API_KEY in .env (cached). 30s request timeout."""
    load_dotenv()
    token = os.getenv("NOTION_API_KEY", "").strip()
    if not token:
        raise RuntimeError(
            "NOTION_API_KEY is empty. Fill .env (the secret is at "
            "https://www.notion.so/profile/integrations)."
        )
    return Client(auth=token, timeout_ms=30_000)


# Validation (400) errors are normally fatal — retrying identical bytes won't help.
# But Notion occasionally returns a *spurious* 400 for a perfectly valid request
# under load (e.g. one row of a batch fails "Invalid ancestor path" while the other
# 200 identical creates succeed). These substrings mark such known-flaky messages so
# `call` retries them as transient instead of surfacing a false "needs a fix".
_SPURIOUS_VALIDATION = (
    "invalid ancestor path",
)


def call(fn, **kwargs):
    """Run a Notion call with retry on rate-limit (429) / server (5xx) / timeout /
    spurious validation, plus a 0.34s throttle (~3 req/s, Notion's average limit).
    Genuine non-transient errors (bad token, not found, real validation) raise at once.
    """
    delay = 1.0
    for _ in range(7):
        try:
            result = fn(**kwargs)
            time.sleep(0.34)
            return result
        except APIResponseError as exc:
            status = getattr(exc, "status", 0)
            spurious = any(s in str(exc).lower() for s in _SPURIOUS_VALIDATION)
            if status != 429 and status < 500 and not spurious:
                raise  # fatal: 401 / 404 / genuine validation
        except (HTTPResponseError, RequestTimeoutError):
            pass
        time.sleep(delay)
        delay = min(delay * 2, 30)
    raise RuntimeError("Notion unavailable after 7 retries")


def extract_id(url_or_id: str) -> str:
    """Pull a UUID out of a Notion URL (or a bare id) as 8-4-4-4-12. Ignores ?v=view."""
    path = url_or_id.strip().split("?", 1)[0].split("#", 1)[0]
    matches = _HEX32.findall(path.replace("-", "").lower())
    if not matches:
        raise ValueError(f"No Notion id found in: {url_or_id!r}")
    raw = matches[-1]
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def resolve_data_source_id(url_or_id: str) -> str:
    """Database id/URL (or a data source id) -> the data source id used to query rows."""
    obj_id = extract_id(url_or_id)
    try:
        db = call(client().databases.retrieve, database_id=obj_id)
    except APIResponseError as err:
        if err.code == "object_not_found":  # maybe it already is a data source id
            call(client().data_sources.retrieve, data_source_id=obj_id)
            return obj_id
        raise
    sources = db.get("data_sources", [])
    if not sources:
        raise ValueError(f"database {obj_id} has no data sources")
    return sources[0]["id"]


def _plain(arr) -> str:
    return "".join(part.get("plain_text", "") for part in (arr or []))


def simplify_value(prop: dict):
    """Turn a Notion property object into a plain Python value."""
    t = prop.get("type")
    v = prop.get(t)
    if t in ("title", "rich_text"):
        return _plain(v)
    if t in ("number", "checkbox", "url", "email", "phone_number"):
        return v
    if t in ("select", "status"):
        return v.get("name") if v else None
    if t == "multi_select":
        return [o.get("name") for o in v or []]
    if t == "date":
        return {"start": v.get("start"), "end": v.get("end")} if v else None
    if t == "people":
        return [p.get("name") or p.get("id") for p in v or []]
    if t == "files":
        return [f.get("name") for f in v or []]
    if t == "relation":
        return [r.get("id") for r in v or []]
    if t in ("created_time", "last_edited_time"):
        return v
    if t == "formula":
        return v.get(v.get("type")) if v else None
    return v


def row_to_dict(page: dict) -> dict:
    """Flat {property name: simple value} plus _id / _url / _title."""
    out = {}
    title = ""
    for name, prop in page.get("properties", {}).items():
        out[name] = simplify_value(prop)
        if prop.get("type") == "title":
            title = out[name]
    out["_id"] = page.get("id")
    out["_url"] = page.get("url")
    out["_title"] = title
    return out


def query_rows(url_or_id: str, *, limit: int | None = None, **filters) -> list[dict]:
    """All rows of a database as flat dicts, paging through everything (with retry)."""
    ds_id = resolve_data_source_id(url_or_id)
    rows: list[dict] = []
    cursor = None
    while True:
        page_size = 100 if limit is None else min(100, limit - len(rows))
        resp = call(client().data_sources.query,
                    data_source_id=ds_id, start_cursor=cursor, page_size=page_size, **filters)
        # Skip trashed/archived pages: they can't be edited and shouldn't be matched.
        rows.extend(row_to_dict(p) for p in resp.get("results", [])
                    if not (p.get("archived") or p.get("in_trash")))
        if limit is not None and len(rows) >= limit:
            return rows[:limit]
        if not resp.get("has_more"):
            return rows
        cursor = resp.get("next_cursor")


def prune_multi_select_options(url_or_id: str, ds_id: str, prop: str, *,
                               dry_run: bool = False) -> list[str]:
    """Drop options of a multi_select property that no row uses any more (e.g. a playlist
    that was emptied, renamed or deleted). Notion's option list REPLACES the set, so we
    resend only the used options (keeping their ids/colors); the unused ones — which have
    zero rows — are removed without touching any row's data. Returns the removed names."""
    ds = call(client().data_sources.retrieve, data_source_id=ds_id)
    opts = ((ds.get("properties", {}).get(prop, {}) or {}).get("multi_select", {}) or {}).get("options", [])
    used = {name for r in query_rows(url_or_id) for name in (r.get(prop) or [])}
    keep = [o for o in opts if o.get("name") in used]
    removed = [o.get("name") for o in opts if o.get("name") not in used]
    if removed and not dry_run:
        call(client().data_sources.update, data_source_id=ds_id,
             properties={prop: {"multi_select": {"options": [
                 {"id": o["id"], "name": o["name"], "color": o.get("color", "default")} for o in keep]}}})
    return removed


TEXT_LIMIT = 2000  # max length of one rich_text / title chunk


def u16_chunks(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split text into pieces Notion will accept, measured the way Notion measures them.

    The API counts a chunk in UTF-16 code units (JavaScript string length), not Python
    characters: an emoji or any other astral char costs 2, not 1. So `text[:2000]` can
    still come back as `content.length should be ≤ 2000, instead was 2001` — exactly the
    "1 rows failed to save" seen on the Pocket Casts descriptions. A surrogate pair is
    never split; an astral char that would straddle the edge moves to the next chunk.
    """
    out: list[str] = []
    buf: list[str] = []
    width = 0
    for ch in text:
        w = 2 if ord(ch) > 0xFFFF else 1
        if width + w > limit:
            out.append("".join(buf))
            buf, width = [], 0
        buf.append(ch)
        width += w
    if buf:
        out.append("".join(buf))
    return out


def u16_cut(text: str, limit: int = TEXT_LIMIT) -> str:
    """`text` capped to a single Notion-sized chunk (see u16_chunks)."""
    chunks = u16_chunks(text or "", limit)
    return chunks[0] if chunks else ""


def rich(text: str) -> dict:
    """A rich_text property value, split into chunks Notion accepts (see u16_chunks)."""
    text = text or ""
    if not text:
        return {"rich_text": []}
    return {"rich_text": [{"type": "text", "text": {"content": c}} for c in u16_chunks(text)]}


def file_prop(url: str, name: str) -> dict:
    """A files property holding one external file (Notion's "picture link"): the URL is
    never uploaded, only referenced. An empty url clears the field.

    Notion only previews such a file as an image when the URL *path* ends in an image
    extension (the entry name is ignored). For an extension-less image URL use
    `imported_file_prop` instead, or the row shows a file chip rather than a thumbnail.
    """
    if not url:
        return {"files": []}
    return {"files": [{"type": "external", "name": (name or "photo")[:100],
                       "external": {"url": url}}]}


def imported_file_prop(url: str, name: str) -> dict:
    """Same, but Notion fetches the URL and hosts the file itself ("indirect import").

    The point is the filename we choose: an extension-less image URL (a YouTube avatar,
    say) previews as a thumbnail once it lands under a `.jpg` name. Costs a round trip
    and workspace storage, so prefer `file_prop` when the URL already ends in .jpg/.png.
    """
    if not url:
        return {"files": []}
    name = (name or "photo")[:100]
    upload = call(client().file_uploads.create,
                  mode="external_url", external_url=url, filename=name)
    # The import is server-side and usually done within a second, but a slow origin can
    # take tens of seconds — poll patiently rather than throw away a perfectly good fetch.
    for attempt in range(40):
        status = call(client().file_uploads.retrieve, file_upload_id=upload["id"])["status"]
        if status == "uploaded":
            return {"files": [{"type": "file_upload", "name": name,
                               "file_upload": {"id": upload["id"]}}]}
        if status != "pending":
            raise RuntimeError(f"file import {status}: {url}")
        if attempt >= 10:
            time.sleep(1)
    raise RuntimeError(f"file import still pending: {url}")


def _norm_write(prop: dict):
    """Reduce a build-time (write-format) property to the same plain shape
    `simplify_value` yields for a stored row, so a freshly-built property can be
    compared against the current row to decide whether a write is even needed.
    Files compare by name only — a URL change under a stable filename is not detected."""
    if not isinstance(prop, dict):
        return prop
    if "title" in prop or "rich_text" in prop:
        parts = prop.get("title") or prop.get("rich_text") or []
        return "".join(p.get("text", {}).get("content", "") for p in parts)
    for key in ("number", "checkbox", "url", "email", "phone_number"):
        if key in prop:
            return prop[key]
    if "select" in prop or "status" in prop:
        v = prop.get("select") or prop.get("status")
        return v.get("name") if v else None
    if "multi_select" in prop:
        return [o.get("name") for o in prop["multi_select"] or []]
    if "date" in prop:
        v = prop["date"]
        return {"start": v.get("start"), "end": v.get("end")} if v else None
    if "files" in prop:
        return [f.get("name") for f in prop["files"] or []]
    if "relation" in prop:
        return [r.get("id") for r in prop["relation"] or []]
    if "people" in prop:
        return [p.get("id") for p in prop["people"] or []]
    return prop


def _cmp_key(v):
    """Order-insensitive comparison key: list-valued props (multi_select, relation,
    files) may be built in a different order than Notion stores them, so sort first."""
    if isinstance(v, list):
        return sorted(str(_cmp_key(x)) for x in v)
    return v


def props_changed(built: dict, row: dict, *, ignore: tuple[str, ...] = ()) -> bool:
    """True if any built (write-format) property differs from the row's current value.
    `ignore` skips housekeeping fields (e.g. 'Last Seen') that are bumped every run and
    would otherwise force a needless rewrite. Lets a sync skip an unchanged row wholesale,
    so the daily report's '~updated' count reflects real changes rather than churn — and
    a manual edit to any field the builder doesn't emit is never touched regardless."""
    for name, prop in built.items():
        if name in ignore:
            continue
        if _cmp_key(_norm_write(prop)) != _cmp_key(row.get(name)):
            return True
    return False
