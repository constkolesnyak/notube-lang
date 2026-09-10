"""User-editable settings: Notion database ids, inbox playlists, language codes.

This is the file to edit when playlists, tags, or database ids change.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Every other module calls load_dotenv() from inside a function, i.e. after import
# time — this file reads the environment *at* import time, so it must load .env first
# or the ids below would always come back empty.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")  # project root

# Notion database ids — the 32-hex string in the database URL. They point at your own
# workspace, so they come from the environment rather than being baked in here; set
# NOTION_CHANNELS_DB and NOTION_VIDEOS_DB in .env (see .env.example).
CHANNELS_DB = os.environ.get("NOTION_CHANNELS_DB", "")    # one row per channel
VIDEOS_DB = os.environ.get("NOTION_VIDEOS_DB", "")  # "YT Videos Backup": one row per video

# Status the review queue lives under: where a channel lands when nothing more
# specific is known (the Manual Inbox playlist, and unsubscribed podcasts). Every
# status named in this file must exist as an option on Channels' Status select.
INBOX_STATUS = "Inbox"

# Inbox playlists: (exact YouTube title, tag to add in Channels or None, Rating or None,
# Status to set or None). Every channel found in these playlists is imported into Channels,
# then the playlist is emptied. The Status is written on the channel's row; None clears
# it on re-sync instead. Add a line to track a new playlist.
#
# The four I-* playlists are the one input queue, split by how soon you mean to watch:
# dropping a video in "9. I-Soon 📥" says "this channel, Status = Soon" and nothing else.
# The second axis a playlist encodes is urgency, not a Rating.
INBOX = [
    ("9. I-Seeing 📥", "Input", None, "Seeing"),
    ("9. I-Next 📥", "Input", None, "Next"),
    ("9. I-Soon 📥", "Input", None, "Soon"),
    ("9. I-Later 📥", "Input", None, "Later"),
    ("9. Feed 📥", "Feed", None, None),
    ("9. asmr 📥", "ASMR", None, None),
    ("9. ASMR 5 📥", "ASMR", "5", None),
    ("9. Manual Inbox 📥", None, None, INBOX_STATUS),
]

# Content-language names the channel classifier (lang.py) may write to Channels' "Lang"
# field. Beyond real languages there are two catch-all buckets so EVERY channel can be
# labelled: Music = no speech (music / dance / ambient), Mixed = polyglot or a language
# outside this set. Keep this set identical to the name list in lang.py RULES.
LANGS = {
    "English", "Japanese", "Korean", "Chinese", "Russian", "French",   # original
    "Spanish", "German", "Italian",
    "Portuguese", "Thai", "Vietnamese", "Indonesian", "Hindi",         # other real languages
    "Arabic", "Turkish", "Polish", "Dutch", "Ukrainian", "Swedish",
    "Music", "Mixed",                                                  # no-speech / polyglot
}

# cl.py: the content languages it can score. The key is the Channels "Lang" value (which is what
# the per-language YT views filter on), the value is the ISO code that Migaku's word list,
# Migaku's tokenizer and YouTube's caption tracks all happen to share. Adding a line here is
# the whole job of scoring a new language — run `uv run python -m notube.cl --check --lang <name>`
# first, which reports whether Migaku actually tokenizes it well enough to trust the numbers.
CL_LANGS = {
    "German": "de",
    "Japanese": "ja",
    "French": "fr",
}
CL_DEFAULT_LANG = "German"

# Community posts watch (posts.py): channels whose "Posts" tab is polled for text
# matching POSTS_TERMS. Signed-out read, so no cookies and no API quota are involved.
# (channel id — the UC… in the channel's externalId, label — what notifications call it)
# Empty by default; add the channels you actually care about, e.g.
#     ("UCXXXXXXXXXXXXXXXXXXXXXX", "Some Band"),
POSTS_WATCH: list[tuple[str, str]] = []

# A post is reported when its text contains ANY of these (compared lowercase, so keep
# them lowercase here). Add a line to widen the net. The useful terms are usually a
# mix of three kinds: a tag the channel itself puts on every post in a series (a tour
# hashtag, say), the place you would actually go, and the exact phrasing the channel
# reuses for the announcement you are waiting for — including in its own language,
# because the non-English post often lands first.
POSTS_TERMS = [
    # "#some_tour_tag",
    # "<your city>", "<your country>",
    # "ticket schedule", "open date",
    # "예매", "티켓",
]

# The subset that means "act now" — a hit raises the notification to high priority.
POSTS_TICKET_TERMS: list[str] = []

# How many pages (~10 posts each) one run may walk back. Paging stops earlier as soon
# as a whole page brings nothing unseen, so a quiet day costs a single request; the cap
# only bounds a catch-up run after downtime.
POSTS_MAX_PAGES = 5

# Post ids remembered per channel (dedup window). Must stay well above
# POSTS_MAX_PAGES * 10, or posts would fall out of memory and be re-reported.
POSTS_SEEN_CAP = 500

# Pocket Casts: every subscribed show becomes a Channels row tagged PODCASTS_TAG, with
# PODCASTS_RATING set on brand-new rows only (None = leave Rating empty). Needs
# POCKETCASTS_EMAIL / POCKETCASTS_PASSWORD in .env; the stage is skipped if unset.
PODCASTS_TAG = "Input"
PODCASTS_RATING = None

# Watch Later: the Data API cannot see WL, so videos_sync reads it via InnerTube
# (Chrome cookies). When True, WL is mirrored into the backup like any other
# playlist and its dead/duplicate entries are pruned once backed up. Needs a
# logged-in browser session; if cookies are unavailable the WL step is skipped
# (a warning) and the rest of the backup still runs.
BACKUP_WATCH_LATER = True
WATCH_LATER_TITLE = "Watch Later"  # playlist label shown on the Notion row
