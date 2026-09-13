"""YouTube access: OAuth credentials, Data API wrappers, and small helpers.

Everything that talks to YouTube's public Data API lives here, plus the link /
thumbnail / availability helpers and the channel-metadata builder used to fill
the Notion "YT ..." fields. The internal InnerTube API (for writing playlists)
is separate, in innertube.py.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build

from notube import common  # noqa: F401  (importing forces IPv4 + socket timeout)

NUM_RETRIES = 5  # googleapiclient backoff for transient network/5xx/429
CHANNEL_PARTS = "snippet,brandingSettings,statistics,contentDetails"

# Read-only access is enough: we read playlists via the Data API and write to
# playlists via InnerTube (cookies), never via the Data API.
SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"  # project root
TOKEN_ENV = "GOOGLE_TOKEN_JSON"
CLIENT_SECRET_ENV = "GOOGLE_CLIENT_SECRET_JSON"


# --------------------------------------------------------------- credentials ---
def _upsert_env(key: str, value: str) -> None:
    line = f"{key}='{value}'"
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    text = pattern.sub(line, text) if pattern.search(text) else (text.rstrip("\n") + "\n" if text else "") + line + "\n"
    ENV_PATH.write_text(text)
    os.chmod(ENV_PATH, 0o600)


def get_credentials() -> Credentials:
    """OAuth credentials from GOOGLE_TOKEN_JSON in .env. Refreshes silently; on a
    missing/expired token without refresh it opens a browser login once."""
    load_dotenv(ENV_PATH)
    raw = os.getenv(TOKEN_ENV)
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES) if raw else None

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None
        else:
            _upsert_env(TOKEN_ENV, creds.to_json())
            return creds

    secret = os.getenv(CLIENT_SECRET_ENV)
    if not secret:
        raise RuntimeError(
            f"YouTube auth missing. Set {CLIENT_SECRET_ENV} in .env, then a browser "
            "login will create the token (or re-auth if the token expired)."
        )
    creds = InstalledAppFlow.from_client_config(json.loads(secret), SCOPES).run_local_server(port=0)
    _upsert_env(TOKEN_ENV, creds.to_json())
    return creds


@lru_cache(maxsize=1)
def client() -> Resource:
    """Cached YouTube Data API client (handles its own credentials)."""
    return build("youtube", "v3", credentials=get_credentials(), cache_discovery=False)


# ----------------------------------------------------------------- Data API ---
def list_my_playlists(yt: Resource) -> Iterator[dict[str, Any]]:
    req = yt.playlists().list(part="snippet,contentDetails,status", mine=True, maxResults=50)
    while req is not None:
        resp = req.execute(num_retries=NUM_RETRIES)
        yield from resp.get("items", [])
        req = yt.playlists().list_next(req, resp)


def list_playlist_items(yt: Resource, playlist_id: str) -> Iterator[dict[str, Any]]:
    req = yt.playlistItems().list(part="snippet,contentDetails,status", playlistId=playlist_id, maxResults=50)
    while req is not None:
        resp = req.execute(num_retries=NUM_RETRIES)
        yield from resp.get("items", [])
        req = yt.playlistItems().list_next(req, resp)


def get_channel(yt: Resource, *, num_retries: int = NUM_RETRIES, **selector) -> dict[str, Any] | None:
    """One channel by a channels.list selector (id=, forHandle=, forUsername=) or None."""
    resp = yt.channels().list(part=CHANNEL_PARTS, maxResults=1, **selector).execute(num_retries=num_retries)
    items = resp.get("items", [])
    return items[0] if items else None


def fetch_channels(yt: Resource, channel_ids: list[str]) -> dict[str, dict]:
    """{channel_id: channel} for many ids, in batches of 50."""
    out: dict[str, dict] = {}
    for i in range(0, len(channel_ids), 50):
        chunk = channel_ids[i:i + 50]
        resp = yt.channels().list(part=CHANNEL_PARTS, id=",".join(chunk), maxResults=50).execute(num_retries=NUM_RETRIES)
        for ch in resp.get("items", []):
            out[ch["id"]] = ch
    return out


def list_featured_channels(yt: Resource, channel_id: str, *, num_retries: int = NUM_RETRIES) -> list[dict]:
    """Channels featured in a channel's "multiple channels" sections: [{id,title,customUrl}]."""
    resp = yt.channelSections().list(part="contentDetails", channelId=channel_id).execute(num_retries=num_retries)
    ids: list[str] = []
    for sec in resp.get("items", []):
        for cid in (sec.get("contentDetails", {}) or {}).get("channels", []) or []:
            if cid not in ids:
                ids.append(cid)
    if not ids:
        return []
    info = yt.channels().list(part="snippet", id=",".join(ids[:50]), maxResults=50).execute(num_retries=num_retries)
    by_id = {c["id"]: {"id": c["id"], "title": c["snippet"].get("title", ""),
                       "customUrl": c["snippet"].get("customUrl", "")} for c in info.get("items", [])}
    return [by_id[cid] for cid in ids if cid in by_id]


def batch_get_video_durations(yt: Resource, video_ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(video_ids), 50):
        chunk = [v for v in video_ids[i:i + 50] if v]
        if not chunk:
            continue
        resp = yt.videos().list(part="contentDetails", id=",".join(chunk), maxResults=50).execute(num_retries=NUM_RETRIES)
        for item in resp.get("items", []):
            out[item["id"]] = item.get("contentDetails", {}).get("duration", "")
    return out


def batch_get_video_meta(yt: Resource, video_ids: list[str]) -> dict[str, dict]:
    """{video_id: {privacy, cid, cname, published, thumb, title}} for many ids (batches
    of 50) via one videos.list(snippet,status) call each. Ids the API does not return —
    deleted, private, or blocked — are simply absent; the caller treats a missing id as
    unavailable. This is the authoritative availability + channel source for playlists
    the Data API can't enumerate directly (e.g. Watch Later)."""
    out: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        chunk = [v for v in video_ids[i:i + 50] if v]
        if not chunk:
            continue
        resp = yt.videos().list(part="snippet,status", id=",".join(chunk), maxResults=50).execute(num_retries=NUM_RETRIES)
        for item in resp.get("items", []):
            sn = item.get("snippet", {})
            out[item["id"]] = {
                "privacy": item.get("status", {}).get("privacyStatus", ""),
                "cid": sn.get("channelId", ""),
                "cname": sn.get("channelTitle", ""),
                "published": sn.get("publishedAt", ""),
                "thumb": pick_thumbnail(sn),
                "title": sn.get("title", ""),
            }
    return out


# ------------------------------------------------------- links / thumbnails ---
def video_link(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else ""


def playlist_link(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def channel_link(channel_id: str) -> str:
    return f"https://www.youtube.com/channel/{channel_id}" if channel_id else ""


def set_video_id(item_id: str) -> str:
    """The setVideoId (an item's position id, needed by InnerTube to remove it),
    decoded from a Data API playlistItem id — which is base64 of "{playlistId}.{setVideoId}".
    Reliable and fully paginated, unlike reading setVideoId from InnerTube directly."""
    pad = "=" * (-len(item_id) % 4)
    try:
        dec = base64.b64decode(item_id + pad).decode("latin1")
    except Exception:  # noqa: BLE001
        return ""
    return dec.rsplit(".", 1)[-1] if "." in dec else ""


def pick_thumbnail(snippet: dict) -> str:
    thumbs = snippet.get("thumbnails") or {}
    for size in ("maxres", "standard", "high", "medium", "default"):
        t = thumbs.get(size)
        if t and t.get("url"):
            return t["url"]
    return ""


# Link / id format for the Channels "Link" field and channel-metadata builder.
YT_RE = re.compile(r"youtube\.com/(@[^/?#]+|c/[^/?#]+|channel/[^/?#]+|user/[^/?#]+)", re.I)
_SCHEME_RE = re.compile(r"^https?://(www\.)?", re.I)


def transform_link(url: str) -> str:
    """Normalise a link. A YouTube channel of any form -> youtube.com/<seg>/videos
    (handle/customUrl percent-decoded); any other URL just loses its scheme."""
    url = (url or "").strip()
    if not url:
        return ""
    m = YT_RE.search(url)
    if m:
        return f"youtube.com/{urllib.parse.unquote(m.group(1))}/videos"
    return _SCHEME_RE.sub("", url)


def videos_url(custom_url: str, channel_id: str) -> str:
    return f"https://www.youtube.com/{custom_url}/videos" if custom_url else f"https://www.youtube.com/channel/{channel_id}/videos"


def build_values(channel: dict, featured: list[dict]) -> dict:
    """The Channels "YT ..." text fields for a channel (from channels.list data)."""
    snippet = channel["snippet"]
    branding = channel.get("brandingSettings", {}).get("channel", {})
    uploads = channel["contentDetails"]["relatedPlaylists"].get("uploads", "")
    others = "\n".join(f"{c['title']} - {videos_url(c['customUrl'], c['id'])} - {c['id']}" for c in featured)
    return {
        "YT Channel ID": channel["id"],
        "YT Keywords": branding.get("keywords", ""),
        "YT Uploads": f"https://www.youtube.com/playlist?list={uploads}" if uploads else "",
        "YT Description": snippet.get("description", ""),
        "YT Channels": others,
    }


# --------------------------------------------------------- availability tag ---
def unavail_reason(title: str, privacy: str) -> str | None:
    """Why a playlist video is unavailable (or None if it is fine)."""
    if title == "Private video" or privacy == "private":
        return "private"
    if title == "Deleted video":
        return "deleted"
    if privacy in ("", "privacyStatusUnspecified"):
        return "unavailable"
    return None


# --------------------------- channel "Links" section (Data API hides it) -------
_ABOUT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_ABOUT_COOKIES = {"SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"}  # skip EU consent redirect


def _extract_json(text: str, anchor: str):
    i = text.find(anchor)
    if i < 0:
        return None
    i = text.find("{", i)
    depth = 0
    j = i
    instr = esc = False
    while j < len(text):
        c = text[j]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
        elif c == '"':
            instr = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[i:j + 1])
        j += 1
    return None


def _find_all(obj, key: str, out: list) -> None:
    if isinstance(obj, dict):
        if key in obj:
            out.append(obj[key])
        for v in obj.values():
            _find_all(v, key, out)
    elif isinstance(obj, list):
        for x in obj:
            _find_all(x, key, out)


def _link_real_url(view_model: dict) -> str | None:
    cmds: list = []
    _find_all(view_model, "urlEndpoint", cmds)
    for c in cmds:
        u = c.get("url")
        if u and "redirect" in u:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query).get("q")
            if q:
                return urllib.parse.unquote(q[0])
        if u:
            return u
    return (view_model.get("link") or {}).get("content")


def channel_links(channel_id: str, *, timeout: float = 20.0) -> list[tuple[str, str]]:
    """The channel's "Links" section as [(title, url)] (the Data API does not expose it).
    Empty on any failure; only raises on a network error."""
    r = httpx.get(
        f"https://www.youtube.com/channel/{channel_id}/about",
        headers={"User-Agent": _ABOUT_UA, "Accept-Language": "en-US,en;q=0.9"},
        cookies=_ABOUT_COOKIES, follow_redirects=True, timeout=timeout,
    )
    if r.status_code != 200:
        return []
    data = _extract_json(r.text, "ytInitialData")
    if data is None:
        return []
    vms: list = []
    _find_all(data, "channelExternalLinkViewModel", vms)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for v in vms:
        url = _link_real_url(v)
        if url and url not in seen:
            seen.add(url)
            out.append(((v.get("title") or {}).get("content") or "", url))
    return out
