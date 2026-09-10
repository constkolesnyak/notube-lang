"""The two Channels columns the daily sync deliberately leaves alone: PC Index and Topic.

    uv run python -m notube.pocketcasts_topics                  # dry run: what would change
    uv run python -m notube.pocketcasts_topics --write
    uv run python -m notube.pocketcasts_topics --write --index  # only the position column
    uv run python -m notube.pocketcasts_topics --write --topics # only the new podcasts' tags

Run by hand. `daily.py` refreshes the `PC *` fields from the subscription list and stops
there; these two are a rank and a judgement, and neither belongs in a nightly job.

**PC Index** is where a show sits in the hand-sorted Podcasts tab, 1 = top — the order
`pocketcasts_order.py` snapshots and `pocketcasts_sort.py` rearranges. It is a *rank*, not a
stored number: Pocket Casts' own `sortPosition` is sparse and renumbered on every write, and
one unsubscribe shifts every show below it. So the whole column is rebuilt from the live list
each run rather than patched, and a row that is no longer subscribed has it cleared — an
index left behind on an unsubscribed show is a claim about a list it is not in.

**Topic** belongs only to German and Japanese shows whose Status is neither Skip nor
Dropped: the ones actually being listened to. That is a rule with two halves and the run
does both. It fills an EMPTY Topic on a show that qualifies — never a full one, because a
row that already carries tags was judged, by hand or by an earlier run, and re-running this
must not re-litigate it. And it CLEARS the Topic of a show that has stopped qualifying,
which is the half no "tag what is empty" pass can reach: Lang and Status change after the
tagging, and stale tags claim a show is still being listened to when it was dropped. So a
normal run is "the podcasts subscribed to since last time" plus "the ones retired since
then", both a handful, and re-running it costs nothing.

The tags come from Claude through the `claude` CLI (the Claude Code subscription — no API
key, no `anthropic` dependency), and two things about that shape the code. Every invocation
drags Claude Code's own ~24k-token harness along irreducibly, so all the new podcasts go in
ONE request rather than one per show; and there is no server-side schema, so the contract is
plain `n: Tag, Tag` lines matched on an **echoed number**, never on position — a reply
missing a line leaves that podcast untagged for the next run instead of shifting everyone
else's tags up by one.

The vocabulary is read off the Notion property itself, so a tag added by hand in Notion is
usable on the next run and there is no second list here to drift out of step. Which also
means this file must never *write* that option list: Notion's multi_select update REPLACES
the set — an option absent from the payload is deleted, taking its value out of every row —
and it ignores a rename of an existing option's name by id. Add tags in the Notion UI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

from notube import (
    common,  # noqa: F401  (importing forces IPv4 + socket timeout)
    config,
    notion,
    pocketcasts,
)

TOPIC_LANGS = {"German", "Japanese"}
TOPIC_SKIP_STATUS = {"Skip", "Dropped"}

MODEL = "opus"
TIMEOUT = 3600        # `claude -p` is minutes per call and its cost is erratic
DESC_CHARS = 420      # enough to place a show; the whole field would be mostly boilerplate
MAX_TAGS = 3
BATCH = 60            # podcasts per request — the invocation is what needs amortising

SYSTEM = (
    "You tag podcasts for a personal media library. You answer only with the requested "
    "lines: no preamble, no explanation, no markdown."
)


# ------------------------------------------------------------------------ rows ---
def podcast_rows():
    """Channels rows that are Pocket Casts shows (they carry a PC ID)."""
    rows = notion.query_rows(config.CHANNELS_DB)
    return [r for r in rows if (r.get("PC ID") or "").strip()]


def wants_topic(row) -> bool:
    return row.get("Lang") in TOPIC_LANGS and row.get("Status") not in TOPIC_SKIP_STATUS


def _set(page_id: str, props: dict) -> None:
    notion.call(notion.client().pages.update, page_id=page_id, properties=props)


# ----------------------------------------------------------------- PC Index ---
def refresh_index(rows, *, write: bool) -> int:
    """Rewrite PC Index from the live subscription list; clear it where unsubscribed."""
    order = {p["uuid"]: i + 1
             for i, p in enumerate(sorted(pocketcasts.list_subscriptions(),
                                          key=pocketcasts.sort_position))}
    jobs = []
    for row in rows:
        want = order.get(row["PC ID"].strip())
        if row.get("PC Index") != want:
            jobs.append((row, want))

    cleared = sum(1 for _, want in jobs if want is None)
    print(f"PC Index: {len(order)} subscribed, {len(jobs)} to change "
          f"({cleared} cleared, {len(jobs) - cleared} renumbered)")
    for row, want in jobs[:10]:
        print(f"    {row['_title'][:52]:<52} {row.get('PC Index')} -> {want}")
    if len(jobs) > 10:
        print(f"    … and {len(jobs) - 10} more")
    if write:
        for row, want in jobs:
            _set(row["_id"], {"PC Index": {"number": want}})
    return len(jobs)


# -------------------------------------------------------------------- Topic ---
def vocabulary(ds_id: str) -> list[str]:
    """The Topic option names, straight off the property. Never written back — see module
    docstring: a partial option list DELETES every option missing from it."""
    ds = notion.call(notion.client().data_sources.retrieve, data_source_id=ds_id)
    prop = ds.get("properties", {}).get("Topic") or {}
    options = (prop.get("multi_select") or {}).get("options") or []
    if not options:
        raise RuntimeError("the Channels database has no Topic options to choose from")
    return [o["name"] for o in options]


def claude_binary() -> str:
    path = os.environ.get("NOTUBE_CLAUDE_BIN") or shutil.which("claude")
    if not path:
        raise RuntimeError(
            "the `claude` CLI isn't on PATH — the tagging runs through the Claude Code "
            "subscription rather than an API key. Use --index to skip it."
        )
    return path


def build_prompt(batch, vocab) -> str:
    """One request: the vocabulary, the rules, then the numbered shows."""
    shows = []
    for n, row in enumerate(batch, start=1):
        desc = re.sub(r"\s+", " ", row.get("PC Description") or "").strip()[:DESC_CHARS]
        shows.append(
            f"{n}. {row['_title']}\n"
            f"   author: {row.get('PC Author') or '—'} | language: {row.get('Lang') or '—'}\n"
            f"   {desc or '—'}"
        )
    return (
        "Tag each podcast below with the topics it is actually about.\n\n"
        "Allowed tags — use these EXACT spellings and nothing else:\n"
        + ", ".join(vocab) + "\n\n"
        "Rules:\n"
        f"- 1 to {MAX_TAGS} tags per podcast, most characteristic first.\n"
        "- Only tags that describe the podcast's own subject. A show is not 'Japan' "
        "because it is in Japanese, nor 'News' because it mentions the news in passing.\n"
        "- 'Talk' is for a show whose subject IS the conversation (two friends, an "
        "interview series), not a label for every podcast.\n"
        "- 'Languages' belongs on a show made for language learners.\n"
        "- Invent nothing: a tag outside the list above is dropped.\n\n"
        "Answer with one line per podcast, its number then its tags:\n"
        "  3: History, Politics\n\n"
        "No other text.\n\n"
        + "\n\n".join(shows)
    )


def ask_claude(prompt: str) -> str:
    done = subprocess.run(
        [claude_binary(), "-p", prompt, "--system-prompt", SYSTEM, "--model", MODEL,
         "--output-format", "json", "--strict-mcp-config",
         "--settings", '{"disableAllHooks":true}'],
        capture_output=True, text=True, timeout=TIMEOUT, check=False,
        # Without this `claude` waits on piped input nobody is writing and can exit 1.
        stdin=subprocess.DEVNULL,
    )
    if done.returncode != 0:
        detail = (done.stderr.strip() or done.stdout.strip() or "no output")[:400]
        raise RuntimeError(f"`claude` exited {done.returncode}: {detail}")
    payload = json.loads(done.stdout)
    if payload.get("is_error"):
        raise RuntimeError(f"claude: {payload.get('result') or payload.get('subtype')}")
    usage = payload.get("usage") or {}
    print(f"    claude: out {usage.get('output_tokens', 0):,} tokens, "
          f"${payload.get('total_cost_usd') or 0:.2f}")
    return payload.get("result", "")


def parse_reply(reply: str, batch, vocab) -> dict[int, list[str]]:
    """`n: Tag, Tag` lines -> {index into batch: tags}. Matched on the echoed number, so a
    dropped line costs that one podcast rather than shifting every tag after it."""
    known = {v.casefold(): v for v in vocab}
    out: dict[int, list[str]] = {}
    for line in reply.splitlines():
        match = re.match(r"\s*(\d+)\s*[:.]\s*(.+)", line)
        if not match:
            continue
        n = int(match.group(1))
        if not 1 <= n <= len(batch) or n - 1 in out:
            continue
        tags, seen = [], set()
        for raw in match.group(2).split(","):
            tag = known.get(raw.strip().strip("*`").casefold())
            if tag and tag not in seen:
                seen.add(tag)
                tags.append(tag)
        if tags:
            out[n - 1] = tags[:MAX_TAGS]
    return out


def clear_disqualified(rows, *, write: bool) -> int:
    """Take Topic off a row that no longer qualifies.

    The other half of the rule, and the half a "tag what is empty" pass cannot reach: a
    show's Lang or Status changes *after* it was tagged (four went to Skip the day this was
    written) and the tags stay behind, claiming the row is still being listened to. Only
    ever a clear, never a re-tag: should the show qualify again later it is simply untagged
    by then, and the pass below picks it up like any other new podcast.
    """
    stale = [r for r in rows if r.get("Topic") and not wants_topic(r)]
    if not stale:
        return 0
    print(f"Topic: {len(stale)} no longer qualify")
    for row in stale:
        why = (row.get("Lang") if row.get("Lang") not in TOPIC_LANGS
               else f"status {row.get('Status')}")
        print(f"    clear  {row['_title'][:52]:<52} {why}  {', '.join(row['Topic'])}")
        if write:
            _set(row["_id"], {"Topic": {"multi_select": []}})
    return len(stale)


def assign_topics(rows, *, write: bool) -> int:
    """Tag the qualifying podcasts that have no Topic yet."""
    todo = [r for r in rows if wants_topic(r) and not r.get("Topic")]
    print(f"Topic: {sum(1 for r in rows if wants_topic(r))} qualify "
          f"(Lang in {'/'.join(sorted(TOPIC_LANGS))}, Status not "
          f"{'/'.join(sorted(TOPIC_SKIP_STATUS))}), {len(todo)} untagged")
    if not todo:
        return 0
    if not write:
        for row in todo[:10]:
            print(f"    would tag  {row['_title'][:60]}")
        if len(todo) > 10:
            print(f"    … and {len(todo) - 10} more")
        return 0

    vocab = vocabulary(notion.resolve_data_source_id(config.CHANNELS_DB))
    done = 0
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        print(f"    request {start // BATCH + 1}: {len(batch)} podcasts")
        tags = parse_reply(ask_claude(build_prompt(batch, vocab)), batch, vocab)
        for i, row in enumerate(batch):
            if i not in tags:
                print(f"    no answer for {row['_title'][:60]} — left for the next run")
                continue
            _set(row["_id"], {"Topic": {"multi_select": [{"name": t} for t in tags[i]]}})
            print(f"    {row['_title'][:52]:<52} {', '.join(tags[i])}")
            done += 1
    return done


# ---------------------------------------------------------------------- CLI ---
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="apply (default: dry run)")
    parser.add_argument("--index", action="store_true", help="only refresh PC Index")
    parser.add_argument("--topics", action="store_true", help="only tag new podcasts")
    args = parser.parse_args()
    both = not (args.index or args.topics)

    rows = podcast_rows()
    print(f"{len(rows)} Pocket Casts rows in Channels")
    if both or args.index:
        refresh_index(rows, write=args.write)
    if both or args.topics:
        clear_disqualified(rows, write=args.write)
        assign_topics(rows, write=args.write)
    if not args.write:
        print("dry run — nothing written (pass --write)")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as err:
        sys.exit(f"error: {err}")
