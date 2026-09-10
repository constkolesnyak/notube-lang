"""Pocket Casts access: login (email+password) and the subscriptions list.

Pocket Casts has no official API, but the modern web player and mobile apps talk to
a stable REST API at https://api.pocketcasts.com. Auth is email+password -> a JWT
bearer token; every other call sends "Authorization: Bearer <token>". No browser or
cookies needed (unlike YouTube/InnerTube) — just httpx + the IPv4 patch in common.
"""

from __future__ import annotations

import os
from functools import lru_cache

import httpx
from dotenv import load_dotenv

from notube import (
    common,  # noqa: F401  (importing forces IPv4 + socket timeout)
    notion,  # for the Notion-sized description cap in build_values
)

BASE_URL = "https://api.pocketcasts.com"
HEADERS = {"User-Agent": "notube/1.0", "Origin": "https://playbeta.pocketcasts.com"}
TRANSIENT = (httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError)

_http = httpx.Client(base_url=BASE_URL, timeout=30.0, headers=HEADERS)


def _fatal(exc: Exception) -> bool:
    """A 4xx is the server rejecting the request itself (bad creds / bad body); retrying
    won't help, so stop at once. Network errors and 5xx fall through to the retry."""
    return isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500


# ---------------------------------------------------------------- credentials ---
def login() -> str:
    """JWT bearer token from POCKETCASTS_EMAIL/PASSWORD in .env. Raises on missing
    creds or a rejected login (Pocket Casts has no API key — it's your account login)."""
    load_dotenv()
    email = os.getenv("POCKETCASTS_EMAIL", "").strip()
    password = os.getenv("POCKETCASTS_PASSWORD", "").strip()
    if not (email and password):
        raise RuntimeError(
            "POCKETCASTS_EMAIL / POCKETCASTS_PASSWORD are empty. Fill .env "
            "(Pocket Casts has no API key — login is your account email + password)."
        )
    body = {"email": email, "password": password, "scope": "webplayer"}

    def _do() -> str:
        r = _http.post("/user/login", json=body)
        r.raise_for_status()
        return r.json()["token"]

    return common.retry(_do, on=TRANSIENT, unless=_fatal)


@lru_cache(maxsize=1)
def client() -> str:
    """Cached bearer token (logs in once per process)."""
    return login()


# ------------------------------------------------------------------- requests ---
def _post(path: str, body: dict) -> dict:
    """Authenticated POST. Re-logs in once on a 401 (expired token), retries transient
    errors, fails fast on any other 4xx.

    The write endpoints (/user/sort, /user/podcast/subscribe, ...) answer 200 with an
    EMPTY body, so parsing unconditionally would raise JSONDecodeError *after* the write
    already landed — the call looks failed while the account has changed. Empty body
    means success: return {}."""

    def _do() -> dict:
        token = client()
        r = _http.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401:  # token expired -> drop it, log in again, try once more
            client.cache_clear()
            token = client()
            r = _http.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json() if r.content else {}

    return common.retry(_do, on=TRANSIENT, unless=_fatal)


def list_subscriptions() -> list[dict]:
    """Subscribed shows: [{uuid, title, author, url (show website), ...}, ...]."""
    return _post("/user/podcast/list", {"v": 1}).get("podcasts", [])


# ---------------------------------------------------------------------- writes ---
# All undocumented (Pocket Casts publishes no API); paths and body shapes were read out
# of the web player bundle, https://static.pocketcasts.com/webplayer/assets/api-*.js —
# grep it for `postWithAuthentication` if one of these ever starts 400ing.

PODCAST_SETTINGS = frozenset({
    "episodesSortOrder", "autoStartFrom", "autoSkipLast", "playbackEffects",
    "playbackSpeed", "showArchived", "autoArchive", "autoArchivePlayed",
})


def set_order(positions: dict[str, int], *, folders: dict[str, int] | None = None,
              check: bool = True) -> None:
    """Write the manual list order, as {podcast uuid: position}.

    The web player calls this `updatePodcastListPositions`. It REPLACES the ordering
    wholesale rather than patching it, so `positions` must name every subscribed show —
    `check` re-reads the subscription list and refuses the call otherwise, which is the
    difference between "moved one show" and "scrambled the shows I forgot to include".
    Positions need not be contiguous; only their relative order is visible in the app.
    """
    if check:
        live = {p["uuid"] for p in list_subscriptions()}
        if missing := live - set(positions):
            raise ValueError(
                f"{len(missing)} subscribed show(s) missing from the order "
                f"(e.g. {sorted(missing)[:3]}) — /user/sort replaces the whole list. "
                f"Pass check=False only if you really mean to omit them."
            )
    _post("/user/sort", {
        "version": 2,
        "model": "webplayer",
        "podcasts": [{"uuid": u, "position": p}
                     for u, p in sorted(positions.items(), key=lambda kv: kv[1])],
        "folders": [{"uuid": u, "position": p} for u, p in (folders or {}).items()],
    })


# A show subscribed since the last sort comes back from /user/podcast/list with NO
# sortPosition key at all — the field appears only once /user/sort has placed it. Reading
# it directly is therefore a KeyError waiting for the first new subscription, which is
# exactly when you least want the ordering tools to fall over.
UNPLACED = 10 ** 6


def sort_position(show: dict) -> int:
    """A show's list position, or UNPLACED if it has never been sorted (sorts last)."""
    return show.get("sortPosition", UNPLACED)


def current_order() -> list[str]:
    """Subscribed uuids in list order (index 0 = top of the Podcasts tab)."""
    return [p["uuid"] for p in sorted(list_subscriptions(), key=sort_position)]


def reorder(uuids: list[str]) -> None:
    """Set the list to exactly this order, renumbering 0..N-1. Renumbering closes any
    gaps left by unsubscribes — the visible order is what you passed, but stored
    positions shift, so a diff against an older snapshot will look noisy."""
    set_order({u: i for i, u in enumerate(uuids)})


def swap(uuid_a: str, uuid_b: str) -> None:
    """Exchange two shows' positions, leaving every other stored position untouched."""
    pos = {p["uuid"]: sort_position(p) for p in list_subscriptions()}
    pos[uuid_a], pos[uuid_b] = pos[uuid_b], pos[uuid_a]
    set_order(pos, check=False)  # built from the live list, complete by construction


def move(uuid: str, to_index: int) -> None:
    """Move one show to `to_index` (0-based position in list order); see `reorder`
    about renumbering."""
    order = current_order()
    order.remove(uuid)
    order.insert(max(0, min(to_index, len(order))), uuid)
    reorder(order)


def subscribe(uuid: str) -> None:
    _post("/user/podcast/subscribe", {"uuid": uuid})


def unsubscribe(uuid: str) -> None:
    _post("/user/podcast/unsubscribe", {"uuid": uuid})


def update_podcast(uuid: str, **settings) -> None:
    """Per-show settings, e.g. update_podcast(u, episodesSortOrder=2, playbackSpeed=1.2).
    episodesSortOrder: 2 = oldest first (working through a course), 3 = newest first."""
    if unknown := set(settings) - PODCAST_SETTINGS:
        raise ValueError(f"unknown setting(s) {sorted(unknown)}; known: {sorted(PODCAST_SETTINGS)}")
    _post("/user/podcast/update", {"uuid": uuid, **settings})


# ------------------------------------------------------------------ derived urls ---
def share_url(uuid: str) -> str:
    """Canonical, stable Pocket Casts link for a show. The uuid in it is the match key
    (analogous to channel/UC... in a YouTube Link)."""
    return f"https://pocketcasts.com/podcasts/{uuid}"


def cover_url(uuid: str) -> str:
    """Show cover art (Pocket Casts serves it from the uuid; 480px webp)."""
    return f"https://static.pocketcasts.com/discover/images/webp/480/{uuid}.webp" if uuid else ""


def _clean(text: str) -> str:
    """Match how Notion stores rich text, so a re-sync compares equal: CRLF -> LF and
    drop zero-width chars (else those rows look 'changed' every run)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for zw in ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"):
        text = text.replace(zw, "")
    return text.strip()


def build_values(show: dict) -> dict:
    """The "PC ..." Channels fields from a subscription dict. Note: Pocket Casts exposes
    no real RSS feed (the list's `webFeed` is a bool, `url` is the show's website), so
    "PC Feed" holds that website URL. Description is normalised + capped to one chunk."""
    uuid = (show.get("uuid") or "").strip()
    return {
        "PC ID": uuid,
        "PC Author": _clean(show.get("author")),
        "PC Description": notion.u16_cut(_clean(show.get("description"))),
        "PC Feed": (show.get("url") or "").strip(),  # show website — PC has no RSS feed url
        "PC Cover": cover_url(uuid),
    }
