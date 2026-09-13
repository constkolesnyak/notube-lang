"""InnerTube: write to YouTube playlists with the logged-in Chrome session.

The Data API charges 50 quota units per playlist edit; instead we drive YouTube's
internal "InnerTube" API with the browser's cookies (same thing the website and
`yt-dlp --cookies-from-browser` use) — no quota, no write-scope OAuth. Used to
empty inbox playlists and to remove duplicate entries.

Removal needs a video's `setVideoId` (its id *within that playlist*). Reading it
back from InnerTube is unreliable (pagination caps at ~100 and can return other
playlists' items), so callers derive `setVideoId` from the Data API item id
instead and only use `remove_videos` here. `playlist_items_page` (first page) is
kept for best-effort emptying.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import threading
import time
from http.cookiejar import MozillaCookieJar

import httpx
from yt_dlp.cookies import extract_cookies_from_browser

from notube.common import retry  # importing common also forces IPv4 + socket timeout


def _browser() -> str:
    """Browser to read cookies from. Override with NOTUBE_BROWSER in .env.
    Useful on hosts where only Chromium / Brave / Firefox is installed."""
    return os.environ.get("NOTUBE_BROWSER", "chrome")


def load_cookie_jar(path: str) -> MozillaCookieJar:
    """Load a Netscape-format cookies.txt (yt-dlp export) as a live jar.

    Produce one via `yt_dlp.cookies.extract_cookies_from_browser(...).save(path,
    ignore_discard=True, ignore_expires=True)` on a host where Chrome is logged
    into YouTube, then point NOTUBE_COOKIES_FILE at it.
    """
    jar = MozillaCookieJar(path)
    jar.load(ignore_discard=True, ignore_expires=True)
    # yt-dlp saves session cookies with expires=0; stdlib's send-time policy would
    # treat those as expired-in-1970 and silently stop sending them (YSC et al).
    for c in jar:
        if not c.expires:
            c.expires = None
    return jar


def save_cookie_jar(cl: httpx.Client) -> None:
    """Persist the client's (rotated) cookies back to NOTUBE_COOKIES_FILE.

    YouTube rotates the short-lived auth cookies (SIDCC, __Secure-*PSIDCC/PSIDTS)
    via Set-Cookie on ordinary responses; httpx extracts them into our jar, and
    saving after every run keeps the file a *live* session instead of a decaying
    snapshot. No-op unless in cookies-file mode (the jar is a MozillaCookieJar).
    """
    path = os.environ.get("NOTUBE_COOKIES_FILE")
    jar = getattr(cl.cookies, "jar", None)
    if not path or not isinstance(jar, MozillaCookieJar):
        return
    import fcntl
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # writers only; readers rely on os.replace
        tmp = path + ".tmp"
        jar.save(tmp, ignore_discard=True, ignore_expires=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


ROTATE_HOSTS = ("accounts.google.com", "accounts.youtube.com")


def rotate_session_tokens(cl: httpx.Client) -> list[str]:
    """Ask Google to mint fresh __Secure-*PSIDTS — the write-auth freshness token.

    youtube.com responses rotate SIDCC/__Secure-*PSIDCC on their own, but PSIDTS
    only rotates through accounts.*/RotateCookies (the call a real browser makes
    every few minutes). Without it the jar keeps READING fine while youtubei
    WRITES start 403ing once PSIDTS ages out — observed at ~16h on 2026-08-02/03;
    a RotateCookies poke healed the 403s immediately. One poke per host per run —
    Google 429s rapid repeats. Returns the hosts that answered 200 and persists
    the jar when any did (the old PSIDTS is invalidated server-side, so a fresh
    one must never be lost to a crash)."""
    ok = []
    for host in ROTATE_HOSTS:
        try:
            r = cl.post(f"https://{host}/RotateCookies",
                        headers={"Content-Type": "application/json",
                                 "Origin": f"https://{host}"},
                        content='[000,"-0000000000000000000"]')
            if r.status_code == 200:
                ok.append(host)
        except (httpx.TransportError, httpx.TimeoutException):
            pass
    if ok:
        save_cookie_jar(cl)
    return ok


ORIGIN = "https://www.youtube.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
ADD_CHUNK = 50  # actions per edit_playlist call
PRIVACY = {"private": "PRIVATE", "unlisted": "UNLISTED", "public": "PUBLIC"}
# Selects a channel's community ("Posts") tab on a browse call — the opaque `params`
# the website's own tab link carries. Stable across channels; re-read it off any
# channel page (tabRenderer.endpoint.browseEndpoint.params) if YouTube ever rotates it.
# base64**url** — the `_` is part of the token, not a `/`. Swap them and YouTube
# silently ignores the params and serves the Home tab, i.e. zero posts and no error.
POSTS_TAB_PARAMS = "EgVwb3N0c_IGBAoCSgA="
POST_URL = "https://www.youtube.com/post/"  # + postId = the post's permalink


class _QuietLogger:
    def debug(self, *a, **k):
        pass
    info = warning = error = debug


def deep_find(obj, key: str, out: list) -> None:
    """Collect every value stored under `key` at any depth of a nested JSON object."""
    if isinstance(obj, dict):
        if key in obj:
            out.append(obj[key])
        for v in obj.values():
            deep_find(v, key, out)
    elif isinstance(obj, list):
        for x in obj:
            deep_find(x, key, out)


def _runs_text(node) -> str:
    """Plain text of a {"runs":[...]} or {"simpleText":...} rich-text node."""
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return node.get("simpleText") or ""
    return "".join(r.get("text", "") for r in (node.get("runs") or []) if isinstance(r, dict))


def _byline_channel(node) -> tuple[str, str]:
    """(channel name, channel id) from a shortBylineText / ownerText node.
    The channel id is the first UC… browseId anywhere inside it (empty if absent,
    e.g. for a private/deleted placeholder that has no owner)."""
    ids: list = []
    deep_find(node, "browseId", ids)
    cid = next((c for c in ids if isinstance(c, str) and c.startswith("UC")), "")
    return _runs_text(node), cid


# Placeholder titles YouTube shows for entries that can't be watched, mapped to a
# reason. The Data API cross-check (see videos_sync) is what actually decides
# availability; this only supplies the private-vs-deleted nuance and a fallback.
_PLACEHOLDER_TITLES = {
    "[private video]": "private",
    "[deleted video]": "deleted",
    "[unavailable video]": "unavailable",
}


def _placeholder_reason(title: str, has_owner: bool, is_playable) -> str | None:
    """Best-effort unavailability from the InnerTube renderer alone (None = looks
    available). Requiring "no owner" for the generic bracketed case avoids
    misreading real titles like "[MV] …" (real videos always carry an owner byline)."""
    t = (title or "").strip()
    hit = _PLACEHOLDER_TITLES.get(t.lower())
    if hit:
        return hit
    if is_playable is False:
        return "unavailable"
    if not has_owner and t.startswith("[") and t.endswith("]"):
        return "unavailable"
    return None


def _strip_brackets(title: str) -> str:
    t = (title or "").strip()
    return t[1:-1].strip() if t.startswith("[") and t.endswith("]") else title


def _pick_thumb(node) -> str:
    """Largest thumbnail URL from a {"thumbnails":[...]} node (last = biggest)."""
    thumbs = [t for t in (node or {}).get("thumbnails") or [] if t.get("url")]
    return thumbs[-1]["url"] if thumbs else ""


def _post_image(post: dict) -> str:
    """Widest image attached to a community post ("" when it has none). Scoped to the
    attachment subtree, so the author avatar elsewhere in the renderer can never be
    mistaken for the attachment."""
    lists: list = []
    deep_find(post.get("backstageAttachment") or {}, "thumbnails", lists)
    best, best_w = "", -1
    for lst in lists:
        for t in lst or []:
            if isinstance(t, dict) and t.get("url") and (t.get("width") or 0) > best_w:
                best, best_w = t["url"], t.get("width") or 0
    return best


AUTHUSER_TRIES = 4  # a browser jar rarely holds more than a handful of logins


def _detect_authuser(cl: httpx.Client, cver: str, sapisid: str) -> str:
    """Which X-Goog-AuthUser index of this cookie jar is our channel.

    NOTUBE_YT_AUTHUSER pins it; otherwise ask each index's account_menu who it is
    and take the one holding the expected channel. Falls back to "0" when the
    channel is unknown (no OAuth, no env) — the historical behaviour."""
    pinned = (os.environ.get("NOTUBE_YT_AUTHUSER") or "").strip()
    want = _expected_channel()
    if not want:
        # Detection needs the OAuth channel; if that lookup hiccups, the pin is the
        # difference between working writes and a silent fallback to the wrong
        # account (index 0 answers 200 for shared surfaces — see class docstring).
        return pinned or "0"
    ctx = {"context": {"client": {"clientName": "WEB", "clientVersion": cver,
                                  "hl": "en", "gl": "US"}}}
    for n in range(AUTHUSER_TRIES):
        try:
            r = cl.post(f"{ORIGIN}/youtubei/v1/account/account_menu?prettyPrint=false",
                        headers={"Content-Type": "application/json",
                                 "Authorization": InnerTube._sapisid_hash(sapisid),
                                 "Origin": ORIGIN, "X-Origin": ORIGIN,
                                 "X-Goog-AuthUser": str(n),
                                 "X-Youtube-Client-Name": "1",
                                 "X-Youtube-Client-Version": cver},
                        content=json.dumps(ctx))
            if r.status_code == 200 and want in r.text:
                if n:
                    print(f"  [innertube] acting as authuser={n} ({want})", flush=True)
                return str(n)
        except (httpx.TransportError, httpx.TimeoutException):
            break
    print(f"  [innertube] channel {want} not found in any authuser 0-{AUTHUSER_TRIES - 1} "
          f"of this cookie jar — falling back to {pinned or '0'}; playlist writes may 403",
          flush=True)
    return pinned or "0"


def _expected_channel() -> str:
    """The channel notube acts as: NOTUBE_YT_CHANNEL_ID, else the OAuth account's
    own channel (the one that owns the playlists we read and empty)."""
    env = (os.environ.get("NOTUBE_YT_CHANNEL_ID") or "").strip()
    if env:
        return env
    try:
        from notube.youtube import client as yt_client
        me = yt_client().channels().list(part="id", mine=True).execute(num_retries=2)
        return (me.get("items") or [{}])[0].get("id", "")
    except Exception:  # noqa: BLE001 — detection is best-effort; caller falls back to 0
        return ""


class InnerTube:
    def __init__(self, client: httpx.Client, api_key: str, client_version: str, sapisid: str,
                 authuser: str = "0"):
        self._cl = client
        self._key = api_key
        self._cver = client_version
        self._sapisid = sapisid
        # Which signed-in account of a multi-login cookie jar to act as. A jar
        # exported from a browser with several Google accounts carries them all,
        # and index 0 is NOT necessarily ours: with the wrong index, reads of our
        # playlists 403 (PERMISSION_DENIED) while reads of shared surfaces like
        # Watch Later happily return the OTHER account's data — silently wrong.
        # That was the 2026-08-02/03 "not emptied" failure. Verified: the same
        # edit_playlist call 403s at authuser=0 and succeeds at authuser=1.
        self._authuser = authuser

    @classmethod
    def from_chrome(cls) -> InnerTube:
        cookies_file = os.environ.get("NOTUBE_COOKIES_FILE")
        if cookies_file:
            # Hand httpx the live jar: rotated Set-Cookie land in it directly, and
            # save_cookie_jar() below writes them back so the file self-sustains.
            jar = load_cookie_jar(cookies_file)
            cookies: object = jar
            names = {c.name: c.value for c in jar if c.domain and "youtube.com" in c.domain}
            source = f"cookies file {cookies_file}"
        else:
            browser = _browser()
            # Optional: read cookies from a custom Chrome/Chromium user-data-dir
            # (e.g. an isolated notube profile on a headless host).
            profile = os.environ.get("NOTUBE_CHROME_PROFILE") or None
            jar = extract_cookies_from_browser(browser, profile=profile, logger=_QuietLogger())
            names = {c.name: c.value for c in jar if c.domain and "youtube.com" in c.domain}
            cookies = names
            source = f"{browser} ({profile})" if profile else browser
        sapisid = names.get("SAPISID") or names.get("__Secure-3PAPISID")
        if not sapisid:
            raise RuntimeError(f"No SAPISID cookie in {source} — log into youtube.com there.")
        cl = httpx.Client(headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                          cookies=cookies, follow_redirects=True, timeout=30.0)
        home = cl.get(ORIGIN + "/").text
        if '"LOGGED_IN":true' not in home:
            raise RuntimeError(f"{source} session is not logged into YouTube.")
        import re
        key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', home).group(1)
        cver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', home).group(1)
        # A dead/consent-walled session raises above, so a good file is never
        # overwritten by a bad state.
        save_cookie_jar(cl)
        return cls(cl, key, cver, sapisid, _detect_authuser(cl, cver, sapisid))

    @staticmethod
    def _sapisid_hash(sapisid: str) -> str:
        ts = int(time.time())
        digest = hashlib.sha1(f"{ts} {sapisid} {ORIGIN}".encode()).hexdigest()
        return f"SAPISIDHASH {ts}_{digest}"

    @classmethod
    def anonymous(cls) -> InnerTube:
        """A signed-out client — for reads that need no account (channel community
        posts). No browser cookies, so nothing here can go stale or block on a
        Keychain prompt; `sapisid=""` makes `_call` omit the Authorization header.

        The consent cookies matter from an EU IP: without them youtube.com 302s to
        consent.youtube.com and `browse` answers 403. The homepage GET is not just
        for the api key — it seeds the visitor cookies (VISITOR_INFO1_LIVE / YSC)
        that InnerTube wants on a signed-out call.
        """
        cl = httpx.Client(headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                          cookies={"SOCS": "CAI", "CONSENT": "YES+cb"},
                          follow_redirects=True, timeout=30.0)
        home = retry(lambda: cl.get(ORIGIN + "/").raise_for_status().text, tries=3,
                     on=(httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError))
        import re
        key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', home)
        cver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', home)
        if not key or not cver:
            raise RuntimeError("youtube.com homepage carried no INNERTUBE_API_KEY / "
                               "CLIENT_VERSION — served a consent or captcha wall?")
        return cls(cl, key.group(1), cver.group(1), "")

    def _auth(self) -> str:
        return self._sapisid_hash(self._sapisid)

    def _call(self, endpoint: str, body: dict) -> dict:
        ctx = {"client": {"clientName": "WEB", "clientVersion": self._cver, "hl": "en", "gl": "US"}}
        content = json.dumps({"context": ctx, **body})

        def once():
            headers = {"Content-Type": "application/json",
                       "Origin": ORIGIN, "X-Origin": ORIGIN,
                       "X-Goog-AuthUser": self._authuser,
                       "X-Youtube-Client-Name": "1", "X-Youtube-Client-Version": self._cver}
            if self._sapisid:  # signed out (anonymous()): no SAPISIDHASH to send
                headers["Authorization"] = self._auth()
            r = self._cl.post(f"{ORIGIN}/youtubei/v1/{endpoint}?key={self._key}&prettyPrint=false",
                              headers=headers, content=content)
            r.raise_for_status()
            return r.json()

        # Retry transient network errors and 5xx; raise 4xx (auth/quota) immediately.
        return retry(once, tries=4,
                     on=(httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError),
                     unless=lambda e: isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500)

    def _reseed_from_chrome(self) -> None:
        """Rebuild the underlying browser session in place (fresh cookies + homepage).

        Browsing the personal auto-playlists (Watch Later / Liked) returns an
        intermittent 404 "Requested entity was not found" that is *sticky per
        session*: once a from_chrome() session lands in the bad bucket, every call
        404s, and none of the cheap levers recover it (re-GETting the homepage,
        swapping the httpx client while keeping cookies, or clearing the visitor
        cookies all keep returning 404 — verified). Key / client-version / auth
        cookies are byte-identical between a good and a bad session, so the split is
        purely server-side bucketing fixed at construction. The only thing that
        recovers is a brand-new from_chrome() draw (empirically ~50-85% land good,
        independent draws), so on 404 we rebuild the whole session and retry."""
        fresh = InnerTube.from_chrome()
        try:
            self._cl.close()
        except Exception:  # noqa: BLE001 — best-effort close of the old pool
            pass
        self._cl, self._key, self._cver, self._sapisid, self._authuser = (
            fresh._cl, fresh._key, fresh._cver, fresh._sapisid, fresh._authuser)

    def reseed(self) -> None:
        """Draw a brand-new browser session, discarding this one. For callers whose
        problem is a *successful* response they cannot believe: the bucketing that
        produces one is fixed when the session is constructed, so re-asking on the same
        session can only repeat it, and only a fresh draw can disagree."""
        self._reseed_from_chrome()

    def create_playlist(self, title: str, video_ids: list[str] | None = None,
                        privacy: str = "PRIVATE") -> str:
        """Create a playlist and return its id. `video_ids` may be empty — the
        website's own "New playlist" button posts playlist/create with an empty
        videoIds, which is how an inbox playlist is born (empty, filled by hand)."""
        video_ids = list(video_ids or [])
        res = self._call("playlist/create", {"title": title, "privacyStatus": privacy, "videoIds": video_ids[:1]})
        pid = res.get("playlistId")
        if not pid:
            raise RuntimeError(f"playlist/create returned no playlistId: {json.dumps(res)[:300]}")
        self.add_videos(pid, video_ids[1:])
        return pid

    def delete_playlist(self, playlist_id: str) -> None:
        """Delete a playlist outright — the website's ⋮ ▸ Delete. Irreversible, and
        it takes whatever is still in the playlist with it. The endpoint answers with
        a command bundle rather than a status field, so the only sure check is the
        caller's: re-list the account's playlists and see the title gone."""
        res = self._call("playlist/delete", {"playlistId": playlist_id})
        if res.get("error"):
            raise RuntimeError(f"playlist/delete failed: {json.dumps(res)[:300]}")

    def add_videos(self, playlist_id: str, video_ids: list[str]) -> None:
        for i in range(0, len(video_ids), ADD_CHUNK):
            chunk = video_ids[i:i + ADD_CHUNK]
            actions = [{"action": "ACTION_ADD_VIDEO", "addedVideoId": v} for v in chunk]
            res = self._call("browse/edit_playlist", {"playlistId": playlist_id, "actions": actions})
            if res.get("status") != "STATUS_SUCCEEDED":
                raise RuntimeError(f"edit_playlist(add) not STATUS_SUCCEEDED: {json.dumps(res)[:300]}")
            print(f"  +{len(chunk)}", flush=True)
            time.sleep(0.4)

    def remove_videos(self, playlist_id: str, set_video_ids: list[str]) -> None:
        """Remove items by setVideoId, in batches."""
        for i in range(0, len(set_video_ids), ADD_CHUNK):
            chunk = set_video_ids[i:i + ADD_CHUNK]
            actions = [{"action": "ACTION_REMOVE_VIDEO", "setVideoId": s} for s in chunk]
            res = self._call("browse/edit_playlist", {"playlistId": playlist_id, "actions": actions})
            if res.get("status") != "STATUS_SUCCEEDED":
                raise RuntimeError(f"edit_playlist(remove) not STATUS_SUCCEEDED: {json.dumps(res)[:300]}")
            print(f"  -{len(chunk)}", flush=True)
            time.sleep(0.4)

    def playlist_items_page(self, playlist_id: str) -> list[tuple[str, str]]:
        """First page of a playlist as [(videoId, setVideoId)] (no continuation).

        YouTube renders items as `playlistVideoRenderer` (videoId+setVideoId together)
        or the newer `richItemRenderer` (separate sub-objects), so pair the first
        videoId with the first setVideoId inside each container. Suggested videos have
        no setVideoId and drop out."""
        res = self._call("browse", {"browseId": "VL" + playlist_id})
        containers: list = []
        deep_find(res, "playlistVideoRenderer", containers)
        deep_find(res, "richItemRenderer", containers)
        out: dict[str, str] = {}
        for c in containers:
            vids: list = []
            svids: list = []
            deep_find(c, "videoId", vids)
            deep_find(c, "setVideoId", svids)
            if vids and svids and vids[0] not in out:
                out[vids[0]] = svids[0]
        return list(out.items())

    def _parse_playlist_page(self, res: dict) -> tuple[list[dict], str]:
        """(items, continuation_token) for one browse response page. Items are the
        real list entries (`playlistVideoRenderer`); suggested videos use other
        renderers and are ignored. token is "" once the last page is reached."""
        renderers: list = []
        deep_find(res, "playlistVideoRenderer", renderers)
        items: list[dict] = []
        for r in renderers:
            if not isinstance(r, dict):
                continue
            vid = r.get("videoId")
            if not vid:
                continue
            cname, cid = _byline_channel(r.get("shortBylineText") or r.get("ownerText") or {})
            raw_title = _runs_text(r.get("title"))
            items.append({
                "video_id": vid,
                "set_video_id": r.get("setVideoId", ""),
                "title": _strip_brackets(raw_title),
                "cid": cid,
                "cname": cname,
                "thumb": _pick_thumb(r.get("thumbnail")),
                "is_playable": bool(r.get("isPlayable")),
                "placeholder": _placeholder_reason(raw_title, bool(cid or cname), r.get("isPlayable")),
            })
        cirs: list = []
        deep_find(res, "continuationItemRenderer", cirs)
        token = ""
        for cir in cirs:
            toks: list = []
            deep_find(cir, "token", toks)
            if toks and isinstance(toks[0], str):
                token = toks[0]
                break
        return items, token

    def list_playlist_full(self, playlist_id: str, *, include_unavailable: bool = True,
                           reseed_tries: int = 8):
        """Yield every entry of a playlist via browse + continuation paging.

        The Data API cannot read some playlists (Watch Later); this reads them the
        way the website does. Each item is a dict: video_id, set_video_id, title,
        cid, cname, thumb, is_playable, placeholder (a best-effort unavailability
        reason from the renderer, or None). Availability is decided authoritatively by
        the caller via the Data API, since InnerTube reports dead WL entries as playable.

        include_unavailable adds params="wgYCCAA=", which reveals hidden placeholders
        on playlists that hide them.

        The personal auto-playlists (Watch Later "WL" / Liked "LL") hit a sticky,
        per-session 404 from a fraction of YouTube's WEB backends (see
        _reseed_from_chrome). The bad backends come in short streaks (~15-25s of
        solid 404s, then a good window), so we retry the opening browse up to
        `reseed_tries` times, rebuilding the session AND sleeping with growing
        backoff between tries — the sleep is what makes the retries straddle a
        streak boundary instead of firing inside one bad window. Other errors
        propagate — nothing is swallowed."""
        body: dict = {"browseId": "VL" + playlist_id}
        if include_unavailable:
            body["params"] = "wgYCCAA="

        res = None
        for attempt in range(reseed_tries):
            try:
                res = self._call("browse", body)
                break
            except httpx.HTTPStatusError as exc:
                sticky_404 = exc.response.status_code == 404 and attempt < reseed_tries - 1
                if not sticky_404:
                    raise
                time.sleep(min(5 + attempt * 3, 15))  # straddle the ~15-25s bad streak
                self._reseed_from_chrome()

        seen_tokens: set[str] = set()
        while True:
            items, token = self._parse_playlist_page(res)
            yield from items
            if not token or token in seen_tokens:
                break
            seen_tokens.add(token)
            time.sleep(0.4)  # be a normal-looking client across pages
            res = self._call("browse", {"continuation": token})

    @staticmethod
    def _parse_post_page(res: dict) -> tuple[list[dict], str]:
        """(posts, continuation_token) for one community-tab browse page. Each post is
        a dict: post_id, text, published, image_url. `published` is what the website
        shows — a relative "1 day ago"; InnerTube exposes no absolute timestamp."""
        renderers: list = []
        deep_find(res, "backstagePostRenderer", renderers)
        posts = []
        for r in renderers:
            pid = r.get("postId")
            if not pid:
                continue
            posts.append({
                "post_id": pid,
                "text": _runs_text(r.get("contentText")),
                "published": _runs_text(r.get("publishedTimeText")),
                "image_url": _post_image(r),
            })
        toks: list = []
        deep_find(res, "continuationCommand", toks)
        token = next((t.get("token") for t in toks if isinstance(t, dict) and t.get("token")), "")
        return posts, token

    def channel_post_pages(self, channel_id: str, *, max_pages: int = 5):
        """Yield the channel's community ("Posts") tab one page at a time, newest
        first (~10 posts per page). Pages, not a flat stream, so a caller can stop
        on the first page that brought nothing new.

        `params` selects the Posts tab — the same opaque value the website's own tab
        link carries. Errors propagate; an empty first page means the tab layout
        moved and is the caller's problem to report, never a silent zero."""
        body: dict = {"browseId": channel_id, "params": POSTS_TAB_PARAMS}
        seen_tokens: set[str] = set()
        for _ in range(max_pages):
            res = self._call("browse", body)
            posts, token = self._parse_post_page(res)
            yield posts
            if not posts or not token or token in seen_tokens:
                break
            seen_tokens.add(token)
            body = {"continuation": token}
            time.sleep(0.5)  # be a normal-looking client across pages

    def empty_playlist(self, playlist_id: str, *, max_rounds: int = 30) -> int:
        """Best-effort: read a page, remove it, repeat until empty. For verified
        emptying use clear_playlist (Data API as source of truth) in channels_sync."""
        total = 0
        for _ in range(max_rounds):
            page = self.playlist_items_page(playlist_id)
            if not page:
                break
            self.remove_videos(playlist_id, [svid for _, svid in page])
            total += len(page)
            time.sleep(0.5)
        return total


_shared: InnerTube | None = None
_shared_error: BaseException | None = None
_shared_anon: InnerTube | None = None


def get_innertube_anon() -> InnerTube:
    """One shared signed-out InnerTube for the whole run. No thread/timeout dance:
    that exists in `get_innertube` only to survive a blocking Chrome cookie read,
    and there are no cookies to read here."""
    global _shared_anon
    if _shared_anon is None:
        _shared_anon = InnerTube.anonymous()
    return _shared_anon


def get_innertube(timeout: float = 60.0) -> InnerTube:
    """One shared InnerTube for the whole run (one Keychain prompt). Acquired under
    a thread timeout so a blocking Chrome cookie / Keychain prompt cannot hang an
    unattended run. Success and failure are both cached (we try exactly once)."""
    global _shared, _shared_error
    if _shared is not None:
        return _shared
    if _shared_error is not None:
        raise _shared_error

    result: dict = {}

    def work():
        try:
            result["it"] = InnerTube.from_chrome()
        except BaseException as exc:  # noqa: BLE001 — propagated to the caller below
            result["err"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        _shared_error = TimeoutError(f"Chrome cookie access timed out after {int(timeout)}s (Keychain prompt?)")
        raise _shared_error
    if "err" in result:
        _shared_error = result["err"]
        raise _shared_error
    _shared = result["it"]
    # One hook via the singleton (never per-client: _reseed_from_chrome swaps _cl
    # in place, and stacked LIFO hooks would overwrite fresh cookies with stale).
    atexit.register(_save_shared_jar)
    # Fresh PSIDTS before this run's writes. Only in cookies-file mode: in profile
    # mode the browser owns the session and rotating here would stale ITS copy.
    # Once per process, deliberately not per reseed (rapid RotateCookies 429s).
    if os.environ.get("NOTUBE_COOKIES_FILE") and not rotate_session_tokens(_shared._cl):
        print("  [innertube] RotateCookies failed — writes may 403 if the jar ages",
              flush=True)
    return _shared


def _save_shared_jar() -> None:
    """Persist late rotations (edit_playlist/browse responses) at process exit."""
    if _shared is not None:
        try:
            save_cookie_jar(_shared._cl)
        except Exception:  # noqa: BLE001 — never let a save break interpreter exit
            pass
