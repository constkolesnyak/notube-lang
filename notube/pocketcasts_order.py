"""Pocket Casts list order: snapshot it, diff it, put it back.

The Podcasts tab is hand-sorted and that order carries meaning, but nothing in Pocket
Casts backs it up and the reordering endpoint is undocumented. This module is the wheel,
already invented:

    uv run python -m notube.pocketcasts_order show                # current order
    uv run python -m notube.pocketcasts_order snapshot            # new dated backup folder
    uv run python -m notube.pocketcasts_order diff                # live vs latest snapshot
    uv run python -m notube.pocketcasts_order restore             # push latest snapshot back
    uv run python -m notube.pocketcasts_order swap 12 34          # exchange two positions

`diff`/`restore` default to the newest snapshot; pass a folder to pick another one.
Snapshots live under $NOTUBE_PC_BACKUP_ROOT (default: run/pocketcasts-backups/) and are
never overwritten — one folder per date.

The API side (the /user/sort quirks, in particular that it replaces the whole ordering)
lives in pocketcasts.py; this module is only files, diffing and the CLI.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from notube import pocketcasts
from notube.common import RUN_DIR

BACKUP_ROOT = Path(os.environ.get(
    "NOTUBE_PC_BACKUP_ROOT",
    RUN_DIR / "pocketcasts-backups",
)).expanduser()
PREFIX = "pocketcasts-backup-"


# ---------------------------------------------------------------- snapshot files ---
def _row(p: dict) -> dict:
    return {
        "sortPosition": pocketcasts.sort_position(p),
        "uuid": p["uuid"],
        "folderUuid": p.get("folderUuid") or "",
        "episodesSortOrder": p["settings"]["episodesSortOrder"]["value"],
        "dateAdded": (p.get("dateAdded") or "")[:10],
        "title": p["title"],
        "author": p.get("author") or "",
    }


def _esc(s: str | None) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def snapshot(date: str | None = None) -> Path:
    """Write a dated snapshot folder and return its path.

    Never overwrites: a second snapshot on the same day lands in `<date>-2`, `-3`, ...
    A snapshot is only worth having if taking a new one cannot destroy the old one —
    the state you most want back is usually the one from ten minutes ago.
    """
    date = date or dt.date.today().isoformat()
    out = BACKUP_ROOT / f"{PREFIX}{date}"
    n = 1
    while out.exists() and any(out.iterdir()):
        n += 1
        out = BACKUP_ROOT / f"{PREFIX}{date}-{n}"
    out.mkdir(parents=True, exist_ok=True)

    raw = pocketcasts._post("/user/podcast/list", {"v": 1})
    subs = sorted(raw["podcasts"], key=pocketcasts.sort_position)

    # the verbatim payload is the source of truth: it also carries every per-show setting
    # (speed, trimSilence, autoArchive...) that the other files drop
    (out / "raw-podcast-list.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True))

    with (out / "order.tsv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(_row(subs[0])), delimiter="\t")
        w.writeheader()
        w.writerows(_row(p) for p in subs)

    (out / "order-uuids.txt").write_text("".join(f"{p['uuid']}\n" for p in subs))

    # OPML is the escape hatch to another app. Pocket Casts does not expose the RSS url
    # in this payload (`url` is the show's website), so an importer matches by title and
    # will miss some shows — good enough as a last resort, not a substitute for the JSON.
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<opml version="1.0">',
             f"  <head><title>Pocket Casts subscriptions {date}</title></head>", "  <body>"]
    lines += [f'    <outline type="rss" text="{_esc(p["title"])}" htmlUrl="{_esc(p.get("url"))}"'
              f' pcUuid="{p["uuid"]}" pcSortPosition="{pocketcasts.sort_position(p)}"/>' for p in subs]
    lines += ["  </body>", "</opml>", ""]
    (out / "subscriptions.opml").write_text("\n".join(lines))

    return out


CANONICAL = re.compile(rf"^{re.escape(PREFIX)}(\d{{4}}-\d{{2}}-\d{{2}})(?:-(\d+))?$")


def latest() -> Path:
    """Newest canonical snapshot: `<prefix><date>` or `<prefix><date>-N`, by date then N.

    Folders with any other suffix (`-pre-inbox`, `-before-cleanup`) are hand-made
    rollback points, often reconstructions of an OLD state written recently — so neither
    name order nor mtime identifies them correctly. They are never auto-selected; pass
    such a folder explicitly to diff/restore.
    """
    found = [(m.group(1), int(m.group(2) or 1), p)
             for p in BACKUP_ROOT.glob(f"{PREFIX}*")
             if (m := CANONICAL.match(p.name)) and (p / "order.tsv").is_file()]
    if not found:
        sys.exit(f"no canonical snapshot under {BACKUP_ROOT} — run `snapshot` first")
    return max(found)[2]


def load(folder: Path) -> dict[str, dict]:
    """uuid -> saved row."""
    with (folder / "order.tsv").open(newline="") as f:
        return {r["uuid"]: r for r in csv.DictReader(f, delimiter="\t")}


# ------------------------------------------------------------------- operations ---
def show() -> None:
    subs = sorted(pocketcasts.list_subscriptions(), key=pocketcasts.sort_position)
    for p in subs:
        r = _row(p)
        print(f"{r['sortPosition']:>4}  {r['dateAdded']}  sort{r['episodesSortOrder']}  "
              f"{r['title'][:60]}")
    print(f"\n{len(subs)} shows")


def diff(folder: Path) -> int:
    """Print what changed since the snapshot. Returns the number of differences."""
    saved = load(folder)
    subs = pocketcasts.list_subscriptions()
    live = {p["uuid"]: p for p in subs}

    gone = [saved[u] for u in saved if u not in live]
    new = sorted((live[u] for u in live if u not in saved), key=pocketcasts.sort_position)
    moved, resorted = [], []
    for u, p in live.items():
        if s := saved.get(u):
            if int(s["sortPosition"]) != pocketcasts.sort_position(p):
                moved.append((s, p))
            if int(s["episodesSortOrder"]) != p["settings"]["episodesSortOrder"]["value"]:
                resorted.append((s, p))

    print(f"snapshot {folder.name}: {len(saved)} shows | live: {len(live)} shows")
    for s in gone:
        print(f"  GONE      pos {s['sortPosition']:>3}  {s['title']}")
    for p in new:
        print(f"  NEW       pos {pocketcasts.sort_position(p):>3}  {p['title']}")
    for s, p in sorted(moved, key=lambda t: pocketcasts.sort_position(t[1])):
        print(f"  MOVED     {s['sortPosition']:>3} -> {pocketcasts.sort_position(p):<3}  {p['title']}")
    for s, p in resorted:
        print(f"  SORTORDER {s['episodesSortOrder']} -> "
              f"{p['settings']['episodesSortOrder']['value']}    {p['title']}")

    n = len(gone) + len(new) + len(moved) + len(resorted)
    if not n:
        print("  identical")
    return n


def restore(folder: Path) -> None:
    """Push a snapshot's order back to the account, then verify it landed."""
    saved = load(folder)
    want = {u: int(r["sortPosition"]) for u, r in saved.items()}
    live = {p["uuid"] for p in pocketcasts.list_subscriptions()}

    # /user/sort replaces the whole ordering, so a mismatched subscription set means the
    # call would either drop shows added since or name shows that are gone. Human's call.
    if live != set(want):
        print(f"REFUSING: live has {len(live)} shows, snapshot {len(want)}")
        print(f"  only live:     {len(live - set(want))}")
        print(f"  only snapshot: {len(set(want) - live)}")
        print("  run `diff` to see them; re-snapshot or fix the subscriptions first")
        sys.exit(1)

    pocketcasts.set_order(want)
    after = {p["uuid"]: pocketcasts.sort_position(p) for p in pocketcasts.list_subscriptions()}
    bad = [u for u in want if after[u] != want[u]]
    print(f"restored from {folder.name}: {len(want) - len(bad)}/{len(want)} positions match")
    if bad:
        sys.exit(1)


def swap(pos_a: int, pos_b: int) -> None:
    by_pos = {pocketcasts.sort_position(p): p for p in pocketcasts.list_subscriptions()}
    for pos in (pos_a, pos_b):
        if pos not in by_pos:
            sys.exit(f"no show at position {pos} (positions need not be contiguous — "
                     f"run `show` to see them)")
    a, b = by_pos[pos_a], by_pos[pos_b]
    pocketcasts.swap(a["uuid"], b["uuid"])
    print(f"{pos_a} <-> {pos_b}:  {a['title'][:45]}  <->  {b['title'][:45]}")


# -------------------------------------------------------------------------- cli ---
def main() -> int:
    ap = argparse.ArgumentParser(description="Pocket Casts list order: snapshot / diff / restore.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="print the current order")
    p_snap = sub.add_parser("snapshot", help="write a new dated backup folder")
    p_snap.add_argument("--date", help="folder date (default: today)")
    for name, helptext in (("diff", "compare live against a snapshot"),
                           ("restore", "push a snapshot's order back")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("folder", nargs="?", type=Path, help="snapshot folder (default: newest)")
    p_swap = sub.add_parser("swap", help="exchange two positions")
    p_swap.add_argument("a", type=int)
    p_swap.add_argument("b", type=int)
    args = ap.parse_args()

    if args.cmd == "show":
        show()
    elif args.cmd == "snapshot":
        print(f"wrote {snapshot(args.date)}")
    elif args.cmd == "diff":
        diff(args.folder or latest())
    elif args.cmd == "restore":
        restore(args.folder or latest())
    elif args.cmd == "swap":
        swap(args.a, args.b)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
