"""Bumparr configuration. Every value is overridable by environment variable.

NOTHING here may default to a specific deployment's host, path, model, or brand.
Bumparr ships an engine, not someone else's install: a default pointing at a real
LAN address would have a fresh deployment quietly sending prompts to whatever
happens to answer at that IP on the user's network.
"""
import os
from pathlib import Path

# Settings are plain, function-named env vars: LLM_BASE, ASSET_ROOT,
# SHARE_STREAM, and so on. No prefix, no product or personal name in any
# variable -- a setting is just its function.
def env(name, default=""):
    v = os.environ.get(name)
    return v if v is not None else default


_here = Path(__file__).resolve().parent
_repo = _here.parent  # repo root (parent of bumparr/)
_root = _here.parent

# Where bumper assets live. On Tower this is the staged share; the backend reads
# video files from here and serves them under /media.
ASSET_ROOT = Path(env("ASSET_ROOT", "/assets"))

# SQLite database (playable registry + channel playout state).
DB_PATH = env("DB", str(_root / "data" / "bumparr.db"))

FRONTEND_DIR = Path(env("FRONTEND", str(_here)))  # secondary font search path

# Local, self-hosted model endpoint used to GENERATE text-card bumpers
# (trivia / PSAs / unexplained numbers). Any OpenAI-compatible endpoint you
# point it at. Card *content* is produced by this model, never hand-authored.
# OPTIONAL. Blank means no model is configured and card generation is simply
# unavailable, which is the correct default: the user points this at their own
# inference endpoint, local or cloud. Never ship a real address here.
LOCAL_LLM_BASE = env("LLM_BASE", "").strip()
LOCAL_LLM_MODEL = env("LLM_MODEL", "").strip()

# Optional stock-footage sources for modern b-roll filler (free tiers, no attribution
# required). Leave blank to skip — the old-film sources (archive.org, Wikimedia) still work.
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")

# Scheduler behaviour.
# Repeat protection. A fixed count cannot work across pool sizes: 4 was fine for
# the 30-item pool this started with and is meaningless at 600, where an item
# could legitimately return after five plays. The cooldown is therefore a SHARE
# of the pool, floored so a tiny pool still varies and capped so a huge one
# always keeps candidates available.
COOLDOWN_RECENT = int(env("COOLDOWN", "4"))   # legacy floor
COOLDOWN_FRACTION = float(env("COOLDOWN_FRACTION", "0.35"))
COOLDOWN_MAX = int(env("COOLDOWN_MAX", "400"))
# How long since last play counts as "fresh". Beyond this an item stops gaining
# further advantage. Ten minutes made a clip seen this morning look exactly as
# stale as one never played, so nothing was ever meaningfully overdue.
RECENCY_HORIZON = float(env("RECENCY_HORIZON", "21600"))  # 6h
RECENCY_MAX_BOOST = float(env("RECENCY_MAX_BOOST", "6.0"))
DEFAULT_VIDEO_DURATION = float(env("DEFAULT_VIDEO_DUR", "30"))
CARD_DEFAULT_DURATION = float(env("CARD_DUR", "14"))
STREAM_DEFAULT_DURATION = float(env("STREAM_DUR", "45"))   # live "window" is FORCED off after this
# Hard cap: no video overstays its welcome. A 3-minute clip still cuts away here,
# so nothing ever feels like an unskippable ad.
VIDEO_MAX_DURATION = float(env("VIDEO_MAX", "75"))

CHANNEL_ID = "bumpers"

# Brand name — the single source of truth for everything user-visible: the slam
# on every clip, station IDs, PSAs, the player logo. A deployment rebrands by
# setting this ONE value. No brand name may be hardcoded anywhere else, in code,
# config, or prose.
#
# The default is deliberately generic rather than any real channel's name: an
# unconfigured install should look unbranded, not like someone else's station.
BRAND = env("BRAND", "TV")

# Display font for the brand/headings. Drop a font file into frontend/fonts/ and
# set this to its filename (e.g. "MyFont.ttf"); empty = system default. Swappable
# like BRAND, so a customer restyles without touching code.
BRAND_FONT = env("BRAND_FONT", "")

# Body/card text font — separate from the decorative brand font. This is the
# highly-legible font used for card content (trivia, facts, PSAs). Ships with a
# clean OFL default; override with a font file in frontend/fonts/.
CARD_FONT = env("CARD_FONT", "card-inter.woff2")

# Public base URL for externally-consumable links (M3U entries, media_url in the
# API). External consumers — ErsatzTV, Dispatcharr, Tunarr, Jellyfin, VLC — need
# ABSOLUTE URLs; a relative path in an M3U is not resolvable, since M3U has no
# base-URL rule. Left blank, Bumparr derives the base from the incoming request,
# which is correct for direct access. Set this when Bumparr sits behind a reverse
# proxy or is reached on a different hostname than it sees itself as
# (e.g. "https://bumparr.example.com").
PUBLIC_BASE_URL = env("PUBLIC_URL", "").rstrip("/")

# Timezone for time-of-day content (the clock card). Containers default to UTC,
# which would silently render a clock several hours wrong, so this is explicit
# rather than inherited. Any IANA name, e.g. "America/New_York".
TIMEZONE = (env("TZ") or os.environ.get("TZ", "")).strip()

# Mount points. Bumparr ships an engine and no media: the user points these at
# their own storage. SOURCE material and OUTPUT are deliberately separate trees,
# so a finished bumper is never mistaken for raw material to re-process, and so
# `download -> cut -> brand -> delete source` has somewhere safe to put results.
VIDEO_DIR = Path(env("VIDEOS", str(ASSET_ROOT)))
IMAGE_DIR = Path(env("IMAGES", str(ASSET_ROOT)))
SOUND_DIR = Path(env("SOUNDS", str(ASSET_ROOT / "music_beds")))
OUTPUT_DIR = Path(env("OUTPUT", str(ASSET_ROOT / "bumpers")))

# Typeface pool. The brand slam's slot-machine roulette flickers through these,
# so the more the user mounts the better it looks; with too few it degrades to a
# static slam rather than a two-font stutter that reads as a glitch.
FONT_DIR = Path(env("FONTS",
                               str(Path(__file__).resolve().parent / "fonts")))
ROULETTE_MIN_FONTS = int(env("ROULETTE_MIN_FONTS", "4"))
# Fraction of bumpers whose slam ROLLS rather than sitting static. Decided once
# per clip when it is minted, so the channel always carries a mix: the roll is a
# treat, and a treat every single time stops being one. Matches the reference
# player's own ratio.
ROULETTE_PROB = float(env("ROULETTE_PROB", "0.43"))

# Minimum aspect ratio for VIDEO bumpers. Phone-shot vertical stock is 9:16
# (0.5625) and never looks like broadcast, however it is framed — a channel is a
# landscape medium. Applies to video only: a portrait STILL, such as a wartime
# poster, is legitimate material and is left alone.
MIN_VIDEO_ASPECT = float(env("MIN_VIDEO_ASPECT", "0.95"))

# Playable types the system understands. 'stream' == live webcam / "window".
PLAYABLE_TYPES = ("video", "card", "stream", "image")
