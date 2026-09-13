"""Rate how hard each channel's German is, from its own transcript — the check on CL.

CL answers "how many of these words have I marked known". That is a claim about the *viewer*,
and it turns out to be only part of difficulty. Measured over 310 German channels against an
independent reading of the transcripts (this file's own judge, run twice on disjoint
excerpts):

    CL vs judged difficulty            spearman -0.67
      on lexis-driven channels (83)    spearman -0.77   CL is near the instrument's ceiling
      on speed-driven channels (39)    spearman -0.04   CL carries no information at all

So the two numbers are not substitutes and the table wants both. Where they disagree is
exactly where the interesting content is: a channel with a high CL and a high Level is fast
or dialectal rather than lexically rich, and one with a low CL and a low Level is simple
speech that happens to use words Migaku has never been shown.

**The judge never sees the channel name, the CL, or anything but two excerpts**, so its
verdict cannot be an echo of what we already believe. It reads text, so it cannot hear speed:
its "speed" verdict means the transcript reads as elided and run-on, and inside that group
what actually predicts its score is sentence length (+0.39), not words per minute (+0.13).

Reliability, measured rather than assumed. Rating the SAME excerpts in eight separate batches
moved a channel by sd 0.22-0.39 points on a 1-10 scale; rating DIFFERENT excerpts of the same
channel agreed at spearman 0.85 with a mean gap of 0.62 points. Two readings are therefore
averaged, which is why READINGS is 2 and not 1. The **driver label is much weaker** — the two
readings agree on it for only 51% of channels — so it is written to Notion only when both
readings name the same one, and left empty otherwise.

    uv run python -m notube.level                  # judge every unjudged German channel, write both
    uv run python -m notube.level --dry-run
    uv run python -m notube.level --force --limit 20

Costs about a cent per channel and a few seconds per batch of 24. Not part of daily.py.
"""
from __future__ import annotations

import argparse
import html
import json
import random
import re
import statistics
import sys
import time
from pathlib import Path

from notube import claude, subs
from notube.common import RUN_DIR
from notube.config import CHANNELS_DB, CL_DEFAULT_LANG, CL_LANGS
from notube.notion import call, client, resolve_data_source_id

LEVEL_FIELD = "Level"
DRIVER_FIELD = "Level Driver"
DRIVERS = ("lexis", "speed", "syntax", "dialect", "topic")

BATCH = 20              # channels per call, plus the repeat anchors
READINGS = 2            # independent excerpt sets per channel; see the docstring
ANCHORS = 4             # channels re-rated in every batch, so drift is measured not assumed
EXCERPT = 700           # characters per excerpt; two per reading
MIN_TEXT = 1500         # a transcript shorter than this is not a sample of the channel
STATE = RUN_DIR / "level_progress.json"

TAG_RE = re.compile(r"<[^>]{1,40}>|\{\\an?\d\}")
ANNOT_RE = re.compile(r"\[[^\]]{0,60}\]|\([^)]{0,60}\)")
INDEX_RE = re.compile(r"\d+")
LINE_RE = re.compile(r"\s*([A-Za-z0-9_]+)\s*:\s*([0-9]+(?:\.[0-9])?)\s*\|\s*(\w+)")

SYSTEM = """You rate how hard German-language YouTube speech is to follow for a non-native
learner. You judge the CONTENT, not the speaker's opinions and not how interesting the subject
is. Excerpts are automatic or human subtitles, so ignore transcription errors, missing
punctuation and stray line breaks — rate the German that was spoken."""

RUBRIC = """Rate each excerpt on this scale, one decimal allowed:

 1-2  A1-A2. Deliberate teaching speech or very simple chat. Present tense, short main
      clauses, the commonest 1000 words, slow and fully explicit.
 3-4  B1. Everyday spontaneous speech about concrete things: gaming, vlogging, cooking,
      reacting. Colloquial but plain; particles and fillers; little subordination.
 5-6  B2. Fluent adult conversation with argument in it: opinion, narration with digressions,
      some abstraction, longer sentences, idiom, occasional specialist word explained in place.
 7-8  C1. Dense informational or analytical speech: history, science, politics, film analysis.
      Nominalisations, embedded clauses, unexplained terminology, fast delivery, allusions.
 9-10 C2. Academic or literary register, heavy technical vocabulary, rhetorical complexity,
      strong dialect, or several speakers talking over each other at speed.

Then name the ONE thing that most drives that number, exactly one of:
    lexis    rare, technical or abstract words
    speed    the sheer rate and density of speech, elision, swallowed words
    syntax   long or nested sentence structure
    dialect  regional accent, heavy slang, non-standard forms
    topic    understanding requires background knowledge more than language

Answer with one line per excerpt and NOTHING else, in the order given:
    <id>: <score> | <driver>
Every id must appear exactly once. No preamble, no commentary, no blank lines."""


# ----------------------------------------------------------------- corpus ---
def transcript(path: Path) -> str:
    """One subtitle file as running text: indices, timestamps, tags and sound annotations out."""
    out = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8", errors="replace").strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not INDEX_RE.fullmatch(ln) and "-->" not in ln]
        if lines:
            text = ANNOT_RE.sub(" ", html.unescape(TAG_RE.sub("", " ".join(lines))))
            if text.strip():
                out.append(text.strip())
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def excerpt(text: str, frac: float, n: int = EXCERPT) -> str:
    """`n` characters from `frac` of the way in, starting at a word boundary."""
    if len(text) <= n:
        return text
    start = max(0, min(int(len(text) * frac), len(text) - n))
    cut = text.find(" ", start)
    return text[(cut if 0 <= cut < start + 40 else start):][:n]


# Two excerpt sets per channel, deliberately from different videos and different offsets: the
# whole point of a second reading is that it is not a re-ask of the same words.
OFFSETS = ((0.33, 0.62), (0.50, 0.80))


def readings(texts: list[str]) -> list[str]:
    """READINGS independent two-excerpt samples of one channel, or fewer if it is short."""
    out = []
    for i in range(READINGS):
        a, b = OFFSETS[i % len(OFFSETS)]
        first = texts[0] if i == 0 else texts[-1]
        second = texts[1] if len(texts) > 1 else texts[0]
        out.append(f"{excerpt(first, a)}\n…\n{excerpt(second, b)}")
    return out


def build_corpus(picks: dict) -> dict[str, list[str]]:
    """{page id: [reading, …]} for every channel with enough transcript to judge."""
    corpus = {}
    for page_id, (_row, picked) in picks.items():
        texts = []
        for video in picked:
            if video["path"] is not None and video["path"].exists():
                text = transcript(video["path"])
                if len(text) >= MIN_TEXT:
                    texts.append(text)
        if texts:
            corpus[page_id] = readings(texts)
    return corpus


# ---------------------------------------------------------------- the ask ---
def parse(reply: str, ids: set[str]) -> dict[str, tuple[float, str]]:
    """`<id>: <score> | <driver>` lines, matched on the echoed id — never by position."""
    got = {}
    for line in reply.splitlines():
        m = LINE_RE.match(line.strip())
        if m and m.group(1) in ids and m.group(3).lower() in DRIVERS:
            got[m.group(1)] = (float(m.group(2)), m.group(3).lower())
    return got


def judge(items: list[tuple[str, str]], what: str
          ) -> tuple[dict[str, tuple[float, str]], float]:
    """({key: (score, driver)}, cost) for one call over (key, text) pairs.

    A reply missing a fifth of the items is asked once more rather than trusted:
    there is no server-side schema, so a short answer is the only signal that
    something went wrong.
    """
    ids = {f"c{i:03d}": key for i, (key, _t) in enumerate(items)}
    texts = dict(items)
    body = "\n\n".join(f"### {cid}\n{texts[key]}" for cid, key in ids.items())
    reply, _usage, cost = claude.ask(f"{RUBRIC}\n\n{body}", SYSTEM, what=what)
    got = parse(reply, set(ids))
    if len(got) < len(ids) * 0.8:
        reply, _usage, more = claude.ask(f"{RUBRIC}\n\n{body}", SYSTEM, what=f"{what} (re-ask)")
        got, cost = parse(reply, set(ids)), cost + more
    return {ids[cid]: value for cid, value in got.items()}, cost


# ----------------------------------------------------------------- notion ---
def ensure_props(ds_id: str) -> None:
    have = call(client().data_sources.retrieve, data_source_id=ds_id).get("properties", {})
    add = {}
    if LEVEL_FIELD not in have:
        add[LEVEL_FIELD] = {"number": {"format": "number"}}
    if DRIVER_FIELD not in have:
        add[DRIVER_FIELD] = {"select": {"options": [{"name": d} for d in DRIVERS]}}
    if add:
        call(client().data_sources.update, data_source_id=ds_id, properties=add)
        print(f"level: created {', '.join(add)}")


def write_row(page_id: str, level: float, driver: str | None) -> None:
    """Level always; the driver only when both readings named the same one."""
    call(client().pages.update, page_id=page_id, properties={
        LEVEL_FIELD: {"number": round(level, 1)},
        DRIVER_FIELD: {"select": {"name": driver} if driver else None},
    })


# ------------------------------------------------------------------- main ---
def main() -> int:
    ap = argparse.ArgumentParser(description="Rate each channel's difficulty from its transcript.")
    ap.add_argument("--lang", default=CL_DEFAULT_LANG, choices=sorted(CL_LANGS))
    ap.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")
    ap.add_argument("--limit", type=int, help="only the first N channels")
    ap.add_argument("--force", action="store_true", help="re-judge channels already done")
    args = ap.parse_args()

    lang, _code = subs.resolve_lang(args.lang)
    print(f"level: {lang} — reusing the subtitle sample on disk, no video is fetched", flush=True)
    picks, _stats, _index, _errors = subs.run(lang=lang, limit=args.limit, reuse=True)
    titles = {pid: (row.get("_title") or "?").strip() for pid, (row, _p) in picks.items()}
    corpus = build_corpus(picks)
    print(f"level: {len(picks)} channels, {len(corpus)} with enough transcript to judge",
          flush=True)
    if not corpus:
        return 0

    done = {} if args.force else (json.loads(STATE.read_text(encoding="utf-8"))
                                  if STATE.exists() else {})
    todo = [pid for pid in corpus if pid not in done]
    # Anchors ride in every batch so cross-batch drift is visible in the state file afterwards.
    anchors = random.Random(7).sample(sorted(corpus), min(ANCHORS, len(corpus)))

    ds_id = resolve_data_source_id(CHANNELS_DB)
    if not args.dry_run:
        ensure_props(ds_id)

    spent = 0.0
    for reading in range(READINGS):
        pending = [pid for pid in todo if len(done.get(pid, {}).get("scores", [])) <= reading]
        batches = [pending[i:i + BATCH] for i in range(0, len(pending), BATCH)]
        for n, batch in enumerate(batches, 1):
            keys = list(dict.fromkeys(batch + anchors))
            items = [(pid, corpus[pid][reading]) for pid in keys]
            started = time.time()
            try:
                got, cost = judge(items, what=f"difficulty batch {n}")
            except Exception as exc:  # noqa: BLE001 — a rate limit costs one batch, not the run
                print(f"  reading {reading + 1} batch {n}: {exc}", flush=True)
                time.sleep(60)
                continue
            spent += cost
            for pid, (score, driver) in got.items():
                entry = done.setdefault(pid, {"title": titles.get(pid, "?"),
                                              "scores": [], "drivers": []})
                entry["scores"].append(score)
                entry["drivers"].append(driver)
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  reading {reading + 1}/{READINGS} batch {n}/{len(batches)}: "
                  f"{len(got)}/{len(keys)} rated, {time.time() - started:.0f}s, "
                  f"${cost:.2f}", flush=True)

    written = 0
    levels = []
    for pid, entry in done.items():
        if not entry["scores"]:
            continue
        level = statistics.mean(entry["scores"])
        agreed = len(set(entry["drivers"])) == 1 and len(entry["drivers"]) >= READINGS
        entry["level"] = round(level, 1)
        entry["driver"] = entry["drivers"][0] if agreed else None
        levels.append(level)
        if not args.dry_run and pid in corpus:
            write_row(pid, level, entry["driver"])
            written += 1
    if not args.dry_run:
        STATE.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")

    spread = [e for e in done.values() if len(e["scores"]) >= 2]
    print(f"\nlevel: judged={len(levels)} written={written} ${spent:.2f}"
          f"{'  (dry run: nothing written)' if args.dry_run else ''}")
    if levels:
        print(f"    level: median={statistics.median(levels):.1f} "
              f"min={min(levels):.1f} max={max(levels):.1f}")
    if spread:
        gaps = [max(e["scores"]) - min(e["scores"]) for e in spread]
        agree = sum(1 for e in spread if len(set(e["drivers"])) == 1)
        print(f"    two readings, {len(spread)} channels: mean gap "
              f"{statistics.mean(gaps):.2f} points, driver agreed on {agree} "
              f"({agree / len(spread):.0%}) — the rest are written with no driver")
    print("    drivers: " + " ".join(
        f"{d}={sum(1 for e in done.values() if e.get('driver') == d)}" for d in DRIVERS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
