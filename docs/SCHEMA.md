# Database schema

One SQLite file at `DB_PATH` (WAL mode, 15s busy timeout — the DB is shared by
the app, the CLI subprocesses, and possibly a co-deployed player). Schema lives
in `bumparr/db.py`; this is the reference for what each column means.

## `playables` — the registry

Every bumper of every type. This is the single source of truth: a file without
a row does not play, and a row is only playable when `enabled=1` and
`health='ok'`.

| Column | Type | Meaning |
|---|---|---|
| `id` | TEXT PK | Stable, namespaced id. Conventions: `vid:<relpath>` (asset-scan), `clip:<stem>:<ts>` (produced), `card:<kind>:…` (cards — content-hash for grounded/seeds, `:seed:` / `:ts:` for generated), `stream:cam:<md5>` (direct cams), `stream:yt:<md5>` (YouTube cams), `img:<relpath>` (still images). |
| `type` | TEXT | `video` \| `card` \| `stream` \| `image`. |
| `kind` | TEXT | The category. Free-form: asset directories map to kinds via `seed.py`'s `CATEGORY` table (or the folder name becomes the kind), cards have their own kinds (`trivia`, `psa`, `number`, `on_this_day`, `weather`, `station_id`, `technical_difficulties`, `dead_air`, …), streams are `webcam`/`window`/…. Kinds are what seasonality, affinity, and pool management operate on. |
| `source` | TEXT | Provenance label: `nasa`, `archive`, `generated`, `seed`, `grounded`, `live-cam`, `youtube-live`, `user-added`, `loc`, `produced`, `render`, …. Descriptive, not load-bearing. |
| `uri` | TEXT | The media pointer. Videos/images: path relative to `ASSET_ROOT`; produced output: `bumpers/<path>` relative to `OUTPUT`; streams: the upstream HLS URL. **NULL until a card is rendered** — that NULL is what keeps unrendered cards out of `/playlist.m3u` and out of `media_url`. |
| `duration` | REAL | Seconds this item occupies the channel. The fill endpoint's contract depends on these being true, so writers set real measured durations (window captures re-probe on every re-capture). |
| `title` | TEXT | Display name. |
| `payload` | TEXT (JSON) | Per-type content. Cards: `lines` / `answer` / `number` / `meaning` / `reveal_after` / optional background query, creator/source/license metadata / `music`. Produced clips: `from`, `window`, `audio`, `slam`, `branded`, `brand`, `base_weight`. Streams: `direct`, `label`, `region`, optional `proxy_hosts` CDN allowlist. |
| `tags` | TEXT | Comma string, freeform. |
| `weight` | REAL | **Declared** editorial weight — the `base` in the rotation model. The system never mutates it; seasonality multiplies at selection time. `0` = deliberately off air. |
| `enabled` | INTEGER | On/off switch. Dated cards (on_this_day) are parked here by the daily rotation; disabling is preferred over deleting because it is reversible. |
| `health` | TEXT | `ok` \| `dead`. Today the asset sweep (`seed.py`) is the only writer of `dead`: it parks a video/image row whose file it can no longer find, `enabled=0, health='dead'`. `/api/pool/revive` re-checks those and restores what is actually fine. The column is also the intended landing place for playback-failure reporting, which nothing ships yet. |
| `fail_count` | INTEGER | Reserved for that same playback-failure reporting — consecutive failures, reset on success. No writer today; `/api/pool/revive` clears it alongside `health` so the pair stays consistent if one arrives. |
| `last_played` | REAL | Unix ts of last air — the station playout writer updates it when an entry starts; it is the recency factor's input. |
| `play_count` | INTEGER | Lifetime plays — station playout increments it when an entry starts; it is the fatigue factor's input (relative to the pool median). |
| `created_at` | REAL | Unix ts. |

### `type='card'` lifecycle

`uri` NULL (payload only, browser-player visible) → `render_cards` writes the
MP4 and sets `uri` + `payload.branded/brand` → the card becomes a normal
playable. Volatile kinds (`local_time`, `weather`) additionally carry a TTL and
are re-rendered by the volatile-refresh loop; their `uri` is set but the file
expires.

### Upsert rule

`db.upsert_playable` is `INSERT OR IGNORE` — atomic and idempotent, so the
asset scan, generators, and a co-deployed player can all reseed concurrently
without UNIQUE-constraint races. Writers that need "did I insert?" check
`rowcount`/`total_changes`.

## `playout` — channel playout cursor

| Column | Meaning |
|---|---|
| `channel_id` | PK; which channel (the registry is per-pool, playout is per-channel). Station values are `station:live` and `station:standby`. |
| `current_id` | The playable currently airing. |
| `started_at` | Unix ts it started. |

The station playout is the shipped writer, upserting one cursor per channel as
entries air. Other players may also write their own channel values. The
rotation model consumes history and the denormalized `last_played`/`play_count`
values.

## `play_history`

| Column | Meaning |
|---|---|
| `id` | autoincrement. |
| `channel_id` | which channel played it; station values are `station:live` and `station:standby`. |
| `playable_id` | the row played. |
| `played_at` | Unix ts. The station writes one row per aired entry; its built-in slate is never recorded. |

Indexed on `(channel_id, played_at DESC)`. This is the raw feed; the
rotation model works off the denormalized `last_played`/`play_count` columns
so selection stays a single-table read.

## Migrations

`db._migrate` is additive-only (create missing columns), run from
`init_db()`, which every entry point calls. Never drop or rename a column in
place — add a migration.
