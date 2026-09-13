"""Score a language's YT channels in Notion by subtitle availability and comprehension level.

For each channel of that language's Channels view which is not Dropped or Skipped, this takes the
3 freshest normal videos (subs.py), reads their subtitles, tokenizes them with Migaku's own
analyzer (migaku_tok.py), compares each word against the user's Migaku vocabulary (migaku.py),
and writes three fields back to the channel row:

    Subs       No / Auto / Manual — the majority verdict over the 3 videos
    CL         share of subtitle words the user already knows, counting every occurrence
    CL Unique  the same over distinct lemmas — a harsher number that spreads channels out

CL answers "how much of what I hear will I understand", CL Unique answers "how much of this
channel's vocabulary do I have". Both are written as fractions because Notion's percent format
multiplies by 100 for display.

A channel with no usable video gets nothing written at all: "Subs = No" is a claim about the
channel, not a place to park "we could not look".

Languages come from config.CL_LANGS; German is the default. Before trusting a new one, run
`--check`: it measures whether Migaku's analyzer lemmatises that language into the same
vocabulary the word list uses, which is the assumption the whole score rests on.

    uv run python -m notube.cl                        # score the default language
    uv run python -m notube.cl --force --reuse        # rescore everything against a changed word list
    uv run python -m notube.cl --lang Japanese --check   # is Japanese trustworthy?
    uv run python -m notube.cl --lang Japanese --dry-run --limit 5

`--force` rescores channels an earlier run already wrote; `--reuse` keeps the subtitle sample
those runs took instead of re-picking each channel's freshest videos. Together they are the
cheap answer to "my vocabulary grew, redo every number": the per-video lemma counts under
run/cl/tokens/ are independent of what the user knows, so an already-sampled channel rescores
with no network call and no tokenizer at all.

Not part of daily.py; this is a heavy backfill.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import random
import re
import statistics
import sys
import traceback
from collections import Counter
from pathlib import Path

from notube import migaku, migaku_tok, subs
from notube.common import RUN_DIR, run_path
from notube.config import CHANNELS_DB, CL_DEFAULT_LANG, CL_LANGS
from notube.notion import call, client, resolve_data_source_id

# --------------------------------------------------------------- settings ---
SUBS_FIELD = "Subs"
CL_FIELD = "CL"
CLU_FIELD = "CL Unique"
SPEED_FIELD = "Speed"
CLC_FIELD = "CL Content"
SUBS_OPTIONS = ("No", "Auto", "Manual")

RANK = {None: 0, "auto": 1, "manual": 2}     # for the majority vote
LABEL = {0: "No", 1: "Auto", 2: "Manual"}
MARK = {None: "-", "auto": "A", "manual": "M"}

SENTENCE_CAP = 300      # characters; keeps a runaway line from becoming one huge token request
# Below this many words a channel's sample is not worth a number. Two things set it. Some
# "German" tracks hold nothing but a supporter credits list — a couple of hundred usernames,
# which no vocabulary contains, so they score near zero and say nothing about the channel. And
# precision: measured over 279 channels, a single video's CL varies by sd 2.7% within its own
# channel, so a 300-word sample carries about +-5 points, which is the whole spread between a
# hard channel and an easy one. subs.SAMPLE_BYTES exists to reach this rather than to refuse:
# a channel short of it is sampled deeper before it is given up on, and after that change the
# number of unscoreable channels went from 18 to 1.
MIN_TOKENS = 2000
TOKENS_DIR = RUN_DIR / "cl" / "tokens"
# CL Unique is a share of *types*, so it falls as the sample grows: 70,436 of the corpus's
# 116,979 lemmas occur exactly once, and that tail grows with length. Measured over 279
# channels, CL Unique as first shipped correlated -0.74 with log(sample size) — it was ranking
# channels by how long their three videos happened to be, and half its ordering (spearman 0.53
# against the fixed-budget version) was that and not vocabulary. Drawing a fixed number of
# running tokens removes the dependence entirely (-0.74 -> +0.07) at 1.1 points of resampling
# noise. Below the budget no CL Unique is written: a number on a different scale from every
# other row is worse than none.
CLU_BUDGET = 2000
# Half of every transcript is closed class — articles, pronouns, prepositions, auxiliaries,
# interjections — and anyone with 9,000 German words knows all of them. They are the same in
# every channel, so they contribute ~50 points of guaranteed score that say nothing about the
# content, and they are why CL lives in 65-91% instead of using its scale. Measured over 283
# channels against an independent reading of the transcripts: restricting to content words
# takes the spread from sd 4.5% to 6.2% and the agreement with judged difficulty from -0.69 to
# -0.73. Dropping adverbs too (nn/v/adj) reaches -0.75, but German adverbs — deswegen, zwar,
# eher — are real vocabulary, and 0.02 is inside the noise of 283 channels; so adverbs stay.
CONTENT_POS = frozenset({"nn", "v", "adj", "adv"})
# The per-video cache stores, per lemma, a part of speech and the analyzer's own spelling —
# neither of which the first version kept. The spelling matters because `migaku.norm` casefolds,
# and German capitalises its nouns: without it a word list built from this cache reads `bild`.
# The sha1 guard only notices the subtitle text changing, so the format needs its own.
CACHE_VERSION = 3
# The share of running tokens whose lemma Migaku's own vocabulary recognises. Below this the
# analyzer and the word list disagree about what a word is, and any score would be measuring
# that disagreement rather than comprehension. Measured at 94% for German.
MIN_VOCAB_HIT = 0.80


# ------------------------------------------------------------ srt -> text ---
TAG_RE = re.compile(r"<[^>]{1,40}>|\{\\an?\d\}")
# [Musik] / [Gelächter] / (lockere Musik) — the caption track describing sound, not speech.
# Measured over the 1,042 files on disk: 21,337 square-bracket spans over 48 distinct strings
# and 1,240 parenthesised ones, 0.40% of all tokens — but 7.1% on the worst channel, which is
# a comprehension score computed against a list of noises. Both forms go.
ANNOT_RE = re.compile(r"\[[^\]]{0,60}\]|\([^)]{0,60}\)")
INDEX_RE = re.compile(r"\d+")
SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")


def srt_cues(path: Path) -> list[str]:
    """The text of every cue: index and timestamp rows dropped, tags stripped."""
    out = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not INDEX_RE.fullmatch(ln) and "-->" not in ln]
        if lines:
            text = ANNOT_RE.sub(" ", html.unescape(TAG_RE.sub("", " ".join(lines))))
            if text.strip():
                out.append(text)
    return out


def sentences(cues: list[str], cap: int = SENTENCE_CAP) -> list[str]:
    """Cues joined and re-split into sentences.

    Cues break mid-sentence ("Weißt du, was er in" / "dieser Szene sagt?"), and
    German lemmatization leans on case and position, so feeding fragments to the analyzer costs
    accuracy. Joining first and splitting on sentence punctuation gives it whole sentences.
    """
    text = re.sub(r"\s+", " ", " ".join(cues)).strip()
    out = []
    for sentence in SENTENCE_RE.split(text):
        while len(sentence) > cap:
            cut = sentence.rfind(" ", 0, cap)
            cut = cut if cut > 0 else cap
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            out.append(sentence)
    return out


# ------------------------------------------------------------ lemma counts ---
def _is_word(token: dict) -> bool:
    """Keep anything with a letter in it. Drops punctuation, spaces, digits and symbols
    without having to trust the analyzer's part-of-speech tags."""
    return any(c.isalpha() for c in token.get("surface", ""))


def lemma_counts(path: Path, code: str) -> dict:
    """{(lemma, part of speech): count} for one subtitle file, cached under run/cl/tokens/.

    The cache additionally keeps the analyzer's own spelling of each lemma, which this return
    value drops: nothing here compares against a capitalised form, but a word list exported from
    the same cache would otherwise read `bild`, which is not a word anyone wants to see.

    The cache holds counts rather than raw tokens: a few thousand entries instead of a token
    dump, and — because it is independent of what the user knows — every CL can be recomputed
    from it in seconds when the Migaku word list changes (`--force`), with no tokenizer.

    A token collapses to its dictForm, or its surface when the analyzer offers no lemma. The
    surface is not kept as a second chance to match: measured against the Migaku vocabulary,
    dictForm alone already accounts for 94% of running tokens.
    """
    vid = path.stem.split("_")[-1].split(".")[0]
    text = sentences(srt_cues(path))
    sha = hashlib.sha1("\n".join(text).encode()).hexdigest()
    cache = TOKENS_DIR / f"{vid}.json"
    if cache.exists():
        blob = json.loads(cache.read_text(encoding="utf-8"))
        if blob.get("sha1") == sha and blob.get("v") == CACHE_VERSION:
            return {(w, p): n for w, p, n, _form in blob["counts"]}

    counts: Counter = Counter()
    spellings: dict = {}
    for line in migaku_tok.tokenize(text, code):
        for token in line:
            if _is_word(token):
                raw = token.get("dictForm") or token["surface"]
                key = (migaku.norm(raw), token.get("pos") or "?")
                counts[key] += 1
                spellings.setdefault(key, Counter())[raw] += 1
    for key in [k for k in counts if not k[0]]:
        counts.pop(key)
        spellings.pop(key, None)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"vid": vid, "srt": path.name, "sha1": sha,
                                 "v": CACHE_VERSION, "total": sum(counts.values()),
                                 "counts": sorted(
                                     [w, p, n, spellings[(w, p)].most_common(1)[0][0]]
                                     for (w, p), n in counts.items())},
                                ensure_ascii=False), encoding="utf-8")
    return dict(counts)


# ---------------------------------------------------------------- scoring ---
def subs_value(picked: list[dict]) -> str:
    """The majority verdict over the FRESHEST subs.N_VIDEOS, ties broken toward the better track.

    Only the freshest three, even when the sample reaches deeper for text (subs.SAMPLE_BYTES):
    "Subs" answers "if I open this channel now, is there a track?", and a majority taken over
    ten videos would answer a different question with the same word.

    The median of the ranks is the majority whenever one exists, and resolves a three-way
    split (manual / auto / none) to the middle instead of erroring.
    """
    ranks = sorted(RANK[v["source"]] for v in picked[:subs.N_VIDEOS])
    return LABEL[ranks[len(ranks) // 2]]


def score(counts: dict, known: set[str],
          seed: str = "") -> tuple[float, float | None, float] | None:
    """(CL, CL Unique) as fractions, or None when the sample is too thin to mean anything.

    `counts` is keyed by (lemma, part of speech); `known` holds lemmas, because Migaku's own
    POS is discarded on the way in — see CONTENT_POS for the one place the tag is used.

    CL counts every occurrence. CL Unique counts distinct lemmas over a fixed CLU_BUDGET
    running tokens drawn from the same pool, so two channels are compared over samples of one
    size (see CLU_BUDGET); the draw is seeded by the channel, so a rescore never moves it on
    its own. CL Content is CL over content words alone.
    """
    total = sum(counts.values())
    if total < MIN_TOKENS:
        return None
    hit = sum(n for (lemma, _pos), n in counts.items() if lemma in known)
    content = {k: n for k, n in counts.items() if k[1] in CONTENT_POS}
    content_total = sum(content.values())
    return (hit / total, clu(counts, known, seed),
            sum(n for (lemma, _pos), n in content.items() if lemma in known) / content_total
            if content_total else None)


def clu(counts: dict, known: set[str], seed: str = "") -> float | None:
    """Known share of the distinct lemmas in CLU_BUDGET running tokens, or None if too few."""
    bag = [lemma for (lemma, _pos), n in counts.items() for _ in range(n)]
    if len(bag) < CLU_BUDGET:
        return None
    types = set(random.Random(seed or "cl").sample(bag, CLU_BUDGET))
    return sum(1 for lemma in types if lemma in known) / len(types)


def channel_counts(picked: list[dict], code: str) -> dict:
    """Lemma counts pooled over a channel's videos.

    Pooled rather than averaged per video, so a 20-minute video weighs more than a 2-minute one.
    """
    total: Counter = Counter()
    for video in picked:
        if video["path"] is not None:
            total.update(lemma_counts(video["path"], code))
    return dict(total)


# ----------------------------------------------------------------- notion ---
def ensure_props(ds_id: str) -> None:
    """Create the three properties if missing.

    The per-property guard is load-bearing: data_sources.update REPLACES a select's whole
    option list, so touching an existing Subs would wipe any option added by hand since.
    """
    have = call(client().data_sources.retrieve, data_source_id=ds_id).get("properties", {})
    add = {}
    if SUBS_FIELD not in have:
        add[SUBS_FIELD] = {"select": {"options": [{"name": n} for n in SUBS_OPTIONS]}}
    for name in (CL_FIELD, CLU_FIELD, CLC_FIELD):
        if name not in have:
            add[name] = {"number": {"format": "percent"}}
    if SPEED_FIELD not in have:
        add[SPEED_FIELD] = {"number": {"format": "number"}}
    if add:
        call(client().data_sources.update, data_source_id=ds_id, properties=add)
        print(f"cl: created {', '.join(add)}")


def speed(picked: list[dict], counts: dict) -> float | None:
    """Spoken words per minute over the sampled videos, or None when no duration is known.

    CL is a claim about vocabulary and is blind to pace: measured over 310 channels its rank
    correlation with words-per-minute is +0.10, and on the 39 channels an independent judge
    twice called speed-driven, CL's correlation with judged difficulty is -0.04 — no
    information at all. Speech rate is already on disk (the video index records each duration),
    so the blind spot costs one division to cover.
    """
    seconds = sum(v.get("duration") or 0 for v in picked if v["path"] is not None)
    return sum(counts.values()) / (seconds / 60) if seconds else None


def write_row(page_id: str, subs_val: str, cl: float | None, clu: float | None,
              wpm: float | None = None, clc: float | None = None) -> None:
    """Set the fields. A None score clears the number rather than leaving a stale one."""
    number = lambda v: {"number": round(v, 4) if v is not None else None}  # noqa: E731
    call(client().pages.update, page_id=page_id, properties={
        SUBS_FIELD: {"select": {"name": subs_val}},
        CL_FIELD: number(cl),
        CLU_FIELD: number(clu),
        CLC_FIELD: number(clc),
        SPEED_FIELD: {"number": round(wpm) if wpm is not None else None},
    })


# ------------------------------------------------------------------- main ---
def progress_path(code: str) -> Path:
    return RUN_DIR / f"cl_progress_{code}.json"


def load_progress(code: str) -> dict:
    path = progress_path(code)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_progress(done: dict, code: str) -> None:
    run_path(progress_path(code).name).write_text(
        json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")


def check(code: str, picks: dict) -> int:
    """Is Migaku's analyzer trustworthy for this language? The gate every score depends on.

    CL assumes the analyzer's lemmas are drawn from the same vocabulary the word list was
    built from. If they are not — a different segmentation, a different dictionary form — then
    words the user genuinely knows keep missing the set and the score measures the mismatch.
    Tokenizing real subtitles and asking how many lemmas Migaku recognises *at all* (known or
    not) tests exactly that, and it is the one check worth repeating for a new language.
    """
    paths = [v["path"] for _row, picked in picks.values() for v in picked if v["path"]]
    if not paths:
        print(f"check: no subtitles downloaded for {code!r} yet — run without --check first",
              file=sys.stderr)
        return 1

    vocab = migaku.vocabulary(code)
    tagged: Counter = Counter()
    for path in paths[:10]:
        tagged.update(lemma_counts(path, code))
    counts: Counter = Counter()
    for (lemma, _pos), n in tagged.items():
        counts[lemma] += n
    total = sum(counts.values())
    hit = sum(n for lemma, n in counts.items() if lemma in vocab)
    uniq_hit = sum(1 for lemma in counts if lemma in vocab)
    rate = hit / total if total else 0.0

    print(f"check {code}: sampled {len(paths[:10])} files, {total} tokens, {len(counts)} lemmas")
    print(f"  migaku vocabulary: {len(vocab)} words")
    print(f"  running-token hit rate: {rate:.1%}  (need >= {MIN_VOCAB_HIT:.0%})")
    print(f"  unique-lemma hit rate:  {uniq_hit / len(counts):.1%}")
    print("\n  most frequent lemmas Migaku does not recognise "
          "(should be names and rare compounds, not everyday words):")
    for lemma, n in sorted(((w, n) for w, n in counts.items() if w not in vocab),
                           key=lambda x: -x[1])[:15]:
        print(f"    {n:4}  {lemma}")

    if rate < MIN_VOCAB_HIT:
        print(f"\ncheck: FAILED — {rate:.1%} of tokens are lemmas Migaku has never heard of. "
              "The analyzer and the word list disagree about this language; scoring it would "
              "produce plausible but wrong numbers.", file=sys.stderr)
        return 1
    print(f"\ncheck: OK — safe to score {code!r}.")
    return 0


def explain(name: str, known: set[str], picks: dict, code: str) -> int:
    """The words a channel uses that the user does not know — the audit trail for a CL."""
    for row, picked in picks.values():
        if name.lower() not in (row.get("_title") or "").lower():
            continue
        counts = channel_counts(picked, code)
        scored = score(counts, known, seed=row["_title"])
        if scored is None:
            print(f"{row['_title']}: not scoreable ({sum(counts.values())} words)")
            return 1
        cl, clu, clc = scored
        print(f"{row['_title']}: CL={cl:.1%} "
              f"CL Content={clc if clc is None else f'{clc:.1%}'} "
              f"CL Unique={clu if clu is None else f'{clu:.1%}'} "
              f"tokens={sum(counts.values())} lemmas={len(counts)}")
        print("\ntop unknown lemmas:")
        unknown: Counter = Counter()
        for (lemma, _pos), n in counts.items():
            if lemma not in known:
                unknown[lemma] += n
        for lemma, n in unknown.most_common(30):
            print(f"  {n:4}  {lemma}")
        return 0
    print(f"no channel matching {name!r}", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Score one language's YT channels: Subs, CL, CL Unique.")
    ap.add_argument("--lang", default=CL_DEFAULT_LANG, choices=sorted(CL_LANGS),
                    help=f"Channels Lang value to score (default {CL_DEFAULT_LANG})")
    ap.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")
    ap.add_argument("--limit", type=int, help="only the first N channels")
    ap.add_argument("--force", action="store_true", help="rescore channels already done")
    ap.add_argument("--reuse", action="store_true",
                    help="keep the subtitle sample an earlier run took; fetch only unsampled "
                         "channels (a rescore against a changed word list costs no network)")
    ap.add_argument("--explain", metavar="CHANNEL", help="show a channel's unknown words")
    ap.add_argument("--check", action="store_true",
                    help="test whether Migaku tokenizes this language well enough to trust")
    args = ap.parse_args()

    lang, code = subs.resolve_lang(args.lang)
    known, info = migaku.known_words(code)
    print(f"cl: {lang} ({code}) — migaku known={info['known']} vocab={info['vocab']} "
          f"profile={info['profile']!r} origin={info['origin']} "
          f"newest={info['newest']:%Y-%m-%d}", flush=True)
    if info["stale"]:
        print("cl: WARNING Migaku has not synced in over a month — open study.migaku.com in "
              "Chrome, or every CL below is understated.", file=sys.stderr)

    print("cl: selecting channels and fetching subtitles", flush=True)
    picks, stats, _index, errors = subs.run(lang=lang, limit=args.limit, reuse=args.reuse)
    if not picks:
        print(f"cl: no {lang} channels match the view filter")
        return 0
    print(subs.format_stats(stats, len(picks), len(errors)), flush=True)

    with migaku_tok.Sidecar([code]):
        if args.check:
            return check(code, picks)
        if args.explain:
            return explain(args.explain, known, picks, code)

        ds_id = resolve_data_source_id(CHANNELS_DB)
        if not args.dry_run:
            ensure_props(ds_id)

        done = {} if args.force else load_progress(code)
        lines, written, skipped, no_video, thin = [], 0, 0, [], []
        cls, clus, subs_counts = [], [], Counter()

        for n, (page_id, (row, picked)) in enumerate(picks.items(), 1):
            title = (row.get("_title") or "?").strip()
            try:
                if not picked:
                    no_video.append(title)
                    continue
                counts = channel_counts(picked, code)
                scored = score(counts, known, seed=title)
                wpm = speed(picked, counts)
                value = subs_value(picked)
                subs_counts[value] += 1
                marks = "".join(MARK[v["source"]] for v in picked)
                if scored is None:      # not scoreable -> Subs is all we honestly know
                    total = sum(counts.values())
                    why = "no subs" if not total else f"only {total} words"
                    thin.append(title)
                    lines.append(f"  {title}: subs={value} [{marks}] cl=- ({why})")
                    if not args.dry_run and page_id not in done:
                        # Clear CL explicitly: a rerun that newly finds the sample too thin
                        # must not leave the number an earlier, wronger run wrote there.
                        write_row(page_id, value, None, None, wpm, None)
                        done[page_id] = {"title": title, "subs": value, "cl": None}
                        written += 1
                    continue
                cl, clu, clc = scored
                cls.append(cl)
                if clu is not None:
                    clus.append(clu)
                lines.append(f"  {title}: subs={value} [{marks}] cl={cl:.1%}"
                             + (f" clc={clc:.1%}" if clc is not None else "")
                             + (f" clu={clu:.1%}" if clu is not None else "")
                             + (f" wpm={wpm:.0f}" if wpm else "")
                             + f" tokens={sum(counts.values())}")
                if args.dry_run:
                    continue
                if page_id in done:
                    skipped += 1
                    continue
                write_row(page_id, value, cl, clu, wpm, clc)
                done[page_id] = {"title": title, "subs": value, "cl": round(cl, 4),
                                 "clu": round(clu, 4) if clu is not None else None,
                                 "clc": round(clc, 4) if clc is not None else None,
                                 "wpm": round(wpm) if wpm is not None else None}
                written += 1
            except Exception:  # noqa: BLE001 — record loudly, keep going, exit 1 at the end
                errors.append((title, "-", traceback.format_exc()))
            if n % 10 == 0:
                if not args.dry_run:
                    save_progress(done, code)
                print(f"  ...{n}/{len(picks)} scored", flush=True)

        if not args.dry_run:
            save_progress(done, code)

    print(f"\ncl: channels={len(picks)} written={written} skipped={skipped} "
          f"no_video={len(no_video)} errors={len(errors)}"
          f"{'  (dry run: nothing written)' if args.dry_run else ''}")
    print("    subs: " + " ".join(f"{k.lower()}={subs_counts[k]}" for k in SUBS_OPTIONS))
    if cls:
        print(f"    cl: median={statistics.median(cls):.1%} "
              f"min={min(cls):.1%} max={max(cls):.1%}  |  "
              f"cl_unique median={statistics.median(clus):.1%}")
    if thin:
        print(f"    subs too thin to score ({len(thin)}): {', '.join(thin)}")
    if no_video:
        print(f"    no normal video in the {subs.SCAN_MAX} most recent uploads "
              f"({len(no_video)}): {', '.join(no_video[:8])}"
              f"{' ...' if len(no_video) > 8 else ''}")
    for line in lines:
        print(line)

    if errors:
        print(f"\n{len(errors)} ERROR(S):", file=sys.stderr)
        for title, vid, tb in errors:
            print(f"\n--- {title} [{vid}] ---\n{tb}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
