"""SQLite storage: the playable registry, channel playout cursor, and play history."""
import sqlite3
import time
from pathlib import Path

from bumparr import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS playables (
  id           TEXT PRIMARY KEY,
  type         TEXT NOT NULL,                 -- video | card | stream | image
  kind         TEXT,                          -- ambient | station_id | trivia | psa | number | webcam | testpattern ...
  source       TEXT,                          -- nasa | archive | generated | local | manual
  uri          TEXT,                          -- media path (video, relative to ASSET_ROOT) or stream URL; NULL for cards
  duration     REAL NOT NULL DEFAULT 14,      -- seconds this item occupies the channel
  title        TEXT,
  payload      TEXT DEFAULT '{}',             -- JSON (card content, overlay hints, stream flags)
  tags         TEXT DEFAULT '',
  weight       REAL NOT NULL DEFAULT 1.0,
  enabled      INTEGER NOT NULL DEFAULT 1,
  health       TEXT NOT NULL DEFAULT 'ok',    -- ok | dead
  fail_count   INTEGER NOT NULL DEFAULT 0,    -- consecutive playback failures; reset on success
  last_played  REAL DEFAULT 0,
  play_count   INTEGER NOT NULL DEFAULT 0,
  created_at   REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS playout (
  channel_id   TEXT PRIMARY KEY,
  current_id   TEXT,
  started_at   REAL
);
CREATE TABLE IF NOT EXISTS play_history (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id   TEXT,
  playable_id  TEXT,
  played_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_history_chan ON play_history(channel_id, played_at DESC);
"""


def conn():
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=15000")
    return c


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def _migrate(c):
    """Additive migrations for databases created before a column existed."""
    have = {r[1] for r in c.execute("PRAGMA table_info(playables)")}
    if "fail_count" not in have:
        c.execute("ALTER TABLE playables ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0")
        c.commit()


def upsert_playable(c, p: dict):
    """Insert a playable if its id is new; leave existing rows untouched.

    Uses INSERT OR IGNORE so it's atomic and idempotent — a check-then-insert
    races on a shared DB (a player and Bumparr may both reseed) and throws UNIQUE
    constraint errors. Returns True only if this call actually inserted the row.
    """
    cur = c.execute(
        """INSERT OR IGNORE INTO playables
           (id, type, kind, source, uri, duration, title, payload, tags, weight, enabled, health, created_at)
           VALUES (:id, :type, :kind, :source, :uri, :duration, :title, :payload, :tags, :weight, 1, 'ok', :created_at)""",
        {
            "payload": "{}",
            "tags": "",
            "weight": 1.0,
            "created_at": time.time(),
            **p,
        },
    )
    return cur.rowcount > 0
