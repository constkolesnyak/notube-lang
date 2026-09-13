"""Watch channel community posts for the terms you care about.

    uv run python -m notube.posts --json

Prints one JSON object: the posts seen for the first time on this run whose text
matches `POSTS_TERMS` (see config.py). Meant for a cron whose agent turns each
entry into a notification — the summarising is deliberately not done here.

Community posts are reachable through neither the Data API (it has no concept of
them) nor a hashtag page (those index videos), so this polls the website's own
InnerTube endpoint. The read is signed out: no cookies to go stale, no quota.

State lives in `run/posts_seen.json`. A first run on empty state *seeds* it and
reports nothing — otherwise the whole visible backlog would be "new".
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback

from notube import common  # noqa: F401  (forces IPv4 + socket timeout)
from notube.common import run_path
from notube.config import (
    POSTS_MAX_PAGES,
    POSTS_SEEN_CAP,
    POSTS_TERMS,
    POSTS_TICKET_TERMS,
    POSTS_WATCH,
)
from notube.innertube import POST_URL, get_innertube_anon

STATE = "posts_seen.json"
STATE_VERSION = 1


def _load_state() -> tuple[dict[str, list[str]], bool, int]:
    """(channel id -> seen post ids, state_existed, last_ok_at).

    A missing file is a first run and seeds. An *unreadable* one is not: silently
    treating corruption as "nothing seen yet" would re-seed, and re-seeding marks
    the current backlog as read — a genuinely new announcement would vanish
    without a trace. That is the one failure this watcher must never have, so a
    damaged file raises instead.
    """
    path = run_path(STATE)
    if not path.exists():
        return {}, False, 0
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"state file {path} is unreadable ({type(exc).__name__}: {exc}). Refusing to "
            f"run: a fresh start here would silently mark every unseen post as read. "
            f"Inspect it, then delete it to deliberately re-seed."
        ) from exc
    if blob.get("version") != STATE_VERSION:
        # A version bump is ours and deliberate, unlike corruption: re-seed, but the
        # caller says so out loud rather than passing it off as a quiet normal run.
        return {}, False, 0
    return ({k: list(v) for k, v in (blob.get("channels") or {}).items()},
            True, int(blob.get("last_ok_at") or 0))


def _save_state(channels: dict[str, list[str]], *, last_ok_at: int) -> None:
    path = run_path(STATE)
    trimmed = {cid: ids[:POSTS_SEEN_CAP] for cid, ids in channels.items()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": STATE_VERSION, "channels": trimmed,
                               "last_ok_at": last_ok_at},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def matched_terms(text: str) -> list[str]:
    low = (text or "").lower()
    return [t for t in POSTS_TERMS if t in low]


def run(*, seed: bool = False, dry_run: bool = False) -> dict:
    """Poll every channel in POSTS_WATCH. Returns
    {"new": [post, …], "scanned": int, "seeded": bool, "errors": [str]}.

    A per-channel failure is collected into `errors` and never aborts the others;
    its `seen` list is left untouched so the next run re-examines those posts.
    """
    channels, existed, last_ok_at = _load_state()
    seeding = seed or not existed
    new: list[dict] = []
    errors: list[str] = []
    scanned = 0

    if not POSTS_WATCH:
        # Nothing to watch is a misconfiguration, not a quiet clean run.
        return {"new": [], "scanned": 0, "seeded": False, "last_ok_at": last_ok_at,
                "errors": ["  config: POSTS_WATCH is empty — nothing is being watched"]}

    for cid, label in POSTS_WATCH:
        seen = channels.setdefault(cid, [])
        known = set(seen)
        fresh: list[dict] = []
        try:
            it = get_innertube_anon()
            pages = 0
            # Not enumerate(): `pages` is read after the loop, and an empty generator
            # must still leave it at 0 rather than unbound.
            for page in it.channel_post_pages(cid, max_pages=POSTS_MAX_PAGES):
                pages += 1  # noqa: SIM113
                if not page:
                    # An empty page one means the tab moved or we were walled off.
                    # Reported as an error, never as a quiet "nothing new" — that
                    # distinction is the whole point of a watcher.
                    if pages == 1:
                        errors.append(f"  {label}: posts tab returned no posts "
                                      f"(layout changed, or signed-out read blocked)")
                    break
                scanned += len(page)
                page_new = [p for p in page if p["post_id"] not in known]
                fresh.extend(page_new)
                if not page_new:
                    break  # a full page of already-seen posts: we have caught up
        except Exception as exc:  # noqa: BLE001 — one channel must not sink the rest
            traceback.print_exc()
            errors.append(f"  {label}: {type(exc).__name__} "
                          f"{str(exc).strip().replace(chr(10), ' ')[:160]}")
            continue

        # Newest first, matching the order YouTube served them.
        channels[cid] = [p["post_id"] for p in fresh] + seen
        if seeding:
            continue
        for p in fresh:
            hits = matched_terms(p["text"])
            if not hits:
                continue
            new.append({
                "channel": label,
                "post_id": p["post_id"],
                "url": POST_URL + p["post_id"],
                "published": p["published"],
                "matched": hits,
                "ticket": any(t in hits for t in POSTS_TICKET_TERMS),
                "text": p["text"],
                "image_url": p["image_url"],
            })

    # The heartbeat only advances on a clean run, so "we have not successfully read
    # the posts since X" is answerable by anything that can stat this file — which is
    # how a run that never happens at all gets noticed by an external watchdog.
    ok_at = int(time.time()) if not errors else last_ok_at
    if not dry_run:
        _save_state(channels, last_ok_at=ok_at)
    return {"new": new, "scanned": scanned, "seeded": seeding,
            "last_ok_at": ok_at, "errors": errors}


def main() -> int:
    ap = argparse.ArgumentParser(description="Report new matching channel community posts.")
    ap.add_argument("--json", action="store_true", help="print the result as JSON (for a cron agent)")
    ap.add_argument("--seed", action="store_true",
                    help="mark everything currently visible as seen and report nothing")
    ap.add_argument("--dry-run", action="store_true", help="do not write the state file")
    args = ap.parse_args()

    # Anything that escapes run() still has to reach the operator as the same shape
    # the caller parses. A traceback on stderr and a bare non-zero exit is exactly
    # the kind of failure that reads as "quiet day" to whoever is skimming.
    try:
        res = run(seed=args.seed, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        res = {"new": [], "scanned": 0, "seeded": False, "last_ok_at": 0,
               "errors": [f"  run aborted: {type(exc).__name__} "
                          f"{str(exc).strip().replace(chr(10), ' ')[:300]}"]}

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1), flush=True)
    else:
        head = (f"POSTS scanned={res['scanned']} new={len(res['new'])}"
                f"{'  (seeded)' if res['seeded'] else ''}{'  (dry-run)' if args.dry_run else ''}")
        print(head, flush=True)
        for p in res["new"]:
            flag = " [TICKETS]" if p["ticket"] else ""
            print(f"\n{p['channel']} · {p['published']}{flag} · {','.join(p['matched'])}\n"
                  f"{p['url']}\n{p['text'][:400]}", flush=True)
        if res["errors"]:
            print(f"\nerrors ({len(res['errors'])}):", flush=True)
            for e in res["errors"]:
                print(e, flush=True)
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
