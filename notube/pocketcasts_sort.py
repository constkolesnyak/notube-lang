"""Keep the German block of the Podcasts tab sorted by episode count, descending.

The list is three parts and only the middle one is sorted:

    0 ..            the head — BBC, In Our Time, the four German learner podcasts
    <block>         everything German, longest archive first          <- this module
    <tail>          the Japanese shelf, in the order it was built

The two shelves are MEMBERSHIP, not position: SHELVES names their shows by uuid, and the
block is defined as everything else. Anything positional is wrong here twice over —
positions are renumbered on every write, and the app pushes its own local ordering on
sync, which is how a run of this ended up with the Japanese shelf scattered through the
middle of the German block. Boundary anchors ("after Auf Deutsch gesagt!, before Nihongo
con Teppei Z") survived the first but not the second: with the anchor sitting in the
middle, 98 German shows counted as tail and were left unsorted while the split still
looked healthy. A uuid set cannot be moved into the wrong shelf by a reorder.

Within a shelf the LIVE order is kept, not the file's — the shelves are hand-arranged and
rearranging one is the user's business, not this module's.

A show subscribed since the last sort has no position at all (see pocketcasts.UNPLACED) —
it joins the block and is placed by its episode count, which is the whole point: subscribe
whenever, run this, it lands where it belongs.

Episode counts are not in /user/podcast/list. They come from the public cache endpoint,
one request per show, so they are cached in run/ and only refetched when older than
EPISODE_TTL_DAYS — a full cold run is ~500 requests.

    uv run python -m notube.pocketcasts_sort            # dry run: what would move
    uv run python -m notube.pocketcasts_sort --write    # apply
    uv run python -m notube.pocketcasts_sort --refresh  # ignore the cached counts
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from notube import (
    common,  # noqa: F401  (importing forces IPv4 + socket timeout)
    pocketcasts,
)

CACHE = common.RUN_DIR / "pocketcasts-episodes.json"
EPISODE_TTL_DAYS = 7
WORKERS = 10

# uuid lists for the head and the tail; the block is every subscription not in either.
SHELVES = Path(__file__).resolve().parent / "data" / "pocketcasts-shelves.json"


# ------------------------------------------------------------------ episode counts ---
def _fetch(uuid: str) -> int | None:
    """Episode count from the public cache. The endpoint 302s to a dated JSON blob, so
    redirects must be followed — without that every call returns an empty 302 body."""
    url = f"https://podcast-api.pocketcasts.com/podcast/full/{uuid}"
    for _ in range(3):
        try:
            r = httpx.get(url, timeout=60, follow_redirects=True,
                          headers={"User-Agent": "notube/1.0"})
            r.raise_for_status()
            return len(r.json()["podcast"]["episodes"])
        except Exception:
            continue
    return None


def episode_counts(uuids: list[str], refresh: bool = False) -> dict[str, int]:
    """uuid -> episode count, cached on disk. A show whose count cannot be fetched keeps
    its cached value if it has one; only a show with no value at all is left out, and the
    caller refuses to sort then — a missing count would silently sort it to the bottom."""
    cache = json.loads(CACHE.read_text()) if CACHE.is_file() else {}
    today = dt.date.today().isoformat()
    stale = (dt.date.today() - dt.timedelta(days=EPISODE_TTL_DAYS)).isoformat()

    todo = [u for u in uuids
            if refresh or u not in cache or cache[u].get("fetched", "") < stale]
    if todo:
        print(f"fetching episode counts for {len(todo)} shows "
              f"({len(uuids) - len(todo)} cached)")
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for uuid, n in zip(todo, ex.map(_fetch, todo)):
                if n is not None:
                    cache[uuid] = {"episodes": n, "fetched": today}
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True))

    return {u: cache[u]["episodes"] for u in uuids if u in cache}


# -------------------------------------------------------------------------- sorting ---
def shelves() -> tuple[set[str], set[str]]:
    """(head uuids, tail uuids) from SHELVES."""
    doc = json.loads(SHELVES.read_text())
    return {s["uuid"] for s in doc["head"]}, {s["uuid"] for s in doc["tail"]}


def split(subs: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """(head, block, tail). A shelf keeps its live order; everything else is the block,
    unplaced shows included."""
    head_ids, tail_ids = shelves()
    if overlap := head_ids & tail_ids:
        raise SystemExit(f"{len(overlap)} show(s) are on both shelves in {SHELVES.name} — "
                         f"refusing to sort")
    ordered = sorted(subs, key=pocketcasts.sort_position)
    head = [p for p in ordered if p["uuid"] in head_ids]
    tail = [p for p in ordered if p["uuid"] in tail_ids]
    block = [p for p in ordered if p["uuid"] not in head_ids | tail_ids]

    # An unsubscribe is fine — the shelf just gets shorter. Everything vanishing is not:
    # it means SHELVES is describing a different account, and sorting would then flatten
    # both hand-made shelves into the block.
    if not head or not tail:
        raise SystemExit(f"none of the {'head' if not head else 'tail'} shows in "
                         f"{SHELVES.name} are subscribed — refusing to sort")
    return head, block, tail


def sort_block(block: list[dict], counts: dict[str, int]) -> list[dict]:
    """Longest archive first; ties by title, so the result is the same every run."""
    return sorted(block, key=lambda p: (-counts[p["uuid"]], p["title"].lower()))


def run(write: bool = False, refresh: bool = False) -> int:
    subs = pocketcasts.list_subscriptions()
    head, block, tail = split(subs)
    counts = episode_counts([p["uuid"] for p in block], refresh)

    if missing := [p for p in block if p["uuid"] not in counts]:
        raise SystemExit(f"no episode count for {len(missing)} show(s), e.g. "
                         f"{[p['title'] for p in missing][:3]} — refusing to sort, "
                         f"they would silently land at the bottom")

    ordered = sort_block(block, counts)
    order = [p["uuid"] for p in head + ordered + tail]
    before = [p["uuid"] for p in sorted(subs, key=pocketcasts.sort_position)]
    moved = sum(1 for a, b in zip(order, before) if a != b)

    print(f"head {len(head)} | block {len(block)} | tail {len(tail)}")
    print(f"block runs {counts[ordered[0]['uuid']]} -> {counts[ordered[-1]['uuid']]} episodes")
    if not moved:
        print("already sorted — nothing to do")
        return 0
    print(f"{moved} shows change position; top of the block:")
    for p in ordered[:5]:
        print(f"  {counts[p['uuid']]:>5}  {p['title'][:60]}")

    if not write:
        print("dry run — pass --write to apply")
        return 0

    pocketcasts.reorder(order)          # renumbers 0..N-1; only relative order is visible
    after = [p["uuid"] for p in sorted(pocketcasts.list_subscriptions(),
                                       key=pocketcasts.sort_position)]
    ok = after == order
    print("applied" if ok else "MISMATCH: the list did not come back in the order sent")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="apply (default: dry run)")
    ap.add_argument("--refresh", action="store_true", help="refetch every episode count")
    args = ap.parse_args()
    return run(write=args.write, refresh=args.refresh)


if __name__ == "__main__":
    raise SystemExit(main())
