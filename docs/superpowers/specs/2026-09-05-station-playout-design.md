# Station Playout: Bumparr as a Live Channel

**Status:** approved design, 2026-09-05. Implementation plan:
`docs/superpowers/plans/2026-09-05-station-playout.md`.

## Why

Bumparr produces bumpers and lets a scheduler decide what airs. That works for
ErsatzTV and Tunarr, which schedule files. It does nothing for Dispatcharr,
which relays live streams into channels, fails over between a channel's
streams when one dies, and publishes M3U/XMLTV/HDHomeRun/Xtream outputs. It
has no file playback and no scheduler; its plugins are action buttons with no
event hooks. Today's INTEGRATION.md advice ("add `/playlist.m3u` as an M3U
source") gives Dispatcharr six hundred five-second files it can only treat as
six hundred dead channels.

The only way Bumparr is first-class in Dispatcharr is to **be a stream**. Two
of them:

- **`live`** — the whole pool, run as a continuous channel. A guide-backed
  "Bumparr TV" channel in Dispatcharr, ErsatzTV, Tunarr, or any HLS player.
- **`standby`** — the same engine restricted to "please stand by" material.
  Added as the **last failover stream on every Dispatcharr channel**, a dead
  provider degrades into a branded standby loop instead of a spinner. This is
  the reason a Dispatcharr user installs Bumparr.

Running its own channel also makes Bumparr the "co-deployed player" the
architecture docs allude to: the first shipped writer of `play_history`,
`last_played`, and `play_count`. The rotation model's recency, affinity, and
fatigue factors are inert on every install today because nothing reports
plays. The playout fixes that as a side effect.

Dayparts ride along: the station should feel programmed by time of day, not
shuffled, and the guide needs honest block titles.

## Non-goals

- No scheduling of third-party content. The station airs the pool only.
- No live-cam relaying inside the playout. `stream` rows are someone else's
  HLS and cannot be spliced into ours; they stay on `/playlist.m3u` and the
  dashboard exactly as today. Window *captures* (the MP4 snapshots) are in.
- No `POST /api/played` for external consumers in this build. ErsatzTV and
  Tunarr do not call back; the playout is the reporter.
- No transcoding in the request path. Ever.

## Spike result (2026-09-05, local)

Three deliberately mismatched clips (1080p with audio, 720p silent,
640x480 at 25 fps with audio) were conformed to one profile, pre-segmented to
MPEG-TS, and spliced into one playlist with `#EXT-X-DISCONTINUITY` between
items. ffmpeg's HLS demuxer decoded the result continuously end to end
(exit 0, full duration reported, frames intact at every boundary). The only
noise was audio timestamp resets at joins, which the discontinuity tag
exists for and which `apad`/`aresample=async=1` in the conform quiets. The
design below therefore uses **pre-conformed segments plus a Python playlist
writer**, not a live ffmpeg loop. Dispatcharr's own ffmpeg proxy uses the
same demuxer; verifying it against a real Dispatcharr instance is the final
acceptance task of the implementation plan, not an assumption.

## Architecture

```
 registry (playables) ──► station/conform.py ──► ASSET_ROOT/.cache/station/<key>/{000.ts…, index.json}
                                 ▲ background job, one-time per (id, mtime, size)
                                 │
 rotation.weights_for ◄── station/playout.py  (virtual clock; timeline per channel)
   × seasons × dayparts            │  writes play_history / last_played / play_count
                                   ▼
        /station/live/index.m3u8   /station/standby/index.m3u8   (sliding window)
        /station/seg/<key>/NNN.ts  (StaticFiles mount)
        /station/channel.m3u       /station/guide.xml
```

Five new modules under `bumparr/station/`, one new top-level module for
dayparts, one YAML file, one rotation factor, routes, docs, and a dashboard
panel.

### 1. Conform (`bumparr/station/conform.py`)

**Purpose.** Make every playable item splice-safe once, off the request path.

**Profile.** One fixed output for every item:

- video: 1920x1080, 30 fps, H.264 High 4.1, `yuv420p`, GOP 60 with
  `sc_threshold 0` (a keyframe every 2 s, nowhere else), 4000k target /
  4500k max / 8000k buffer;
- audio: AAC-LC, 48 kHz stereo, 128k. Sources with no audio get a synthesized
  silent track (`anullsrc`); sources with audio are `apad`ded to video
  length and `aresample=async=1`;
- letterbox/pillarbox to fit (`scale=…:force_original_aspect_ratio=decrease,pad=…`),
  never crop, never stretch;
- container: MPEG-TS, cut into segments of `STATION_SEGMENT_SECONDS`
  (default 4) with ffmpeg's `segment` muxer, so every segment starts on a
  keyframe.

**Eligibility.** `enabled=1 AND health='ok' AND type IN ('video','card','image')
AND uri IS NOT NULL AND duration > 0`. `image` rows are rendered as a still
for their `duration` with silence. `stream` rows are never conformed.

**Cache layout.** `ASSET_ROOT/.cache/station/<key>/` where
`key = sha256(id + "\0" + mtime_ns + "\0" + size)[:24]`. Inside:
`000.ts, 001.ts, …` and `index.json`:

```json
{"id": "card:trivia:…", "key": "…", "source_mtime_ns": 1725500000000000000,
 "source_size": 123456, "segments": [4.0, 4.0, 2.07], "duration": 10.07,
 "conformed_at": 1725500123.4}
```

`duration` is the conformed duration, which is what the playout uses. It is
the truth the timeline runs on; the registry's `duration` stays what the
producer measured.

**Staleness.** A changed `(mtime, size)` yields a new key; the old directory
is removed after the new one lands. Rows that are deleted, disabled, or
parked have their directory removed on the next sweep. The sweep is
idempotent and safe to run any time. Work is done into `<key>.part/` and
renamed into place, matching the atomic-landing convention used elsewhere.

**Job.** `conform.sweep(limit=None)` conforms every eligible row missing a
current cache entry, oldest-first, and prunes stale/orphan directories. It
runs from `jobs.station_conform_loop()` on startup and then every
`STATION_CONFORM_INTERVAL` seconds (default 300), guarded like the other
loops: exceptions logged, only `CancelledError` exits. Each ffmpeg call uses
the shared `ffmpeg_pipe`-style discipline: `communicate(timeout=…)`, kill
and reap on timeout, remove the partial. The API also exposes it as
`POST /api/station/conform` through the existing background-job mechanism,
so the dashboard can trigger a pass and the operator can see progress.

**Concurrency.** One conform at a time (a module-level lock), because it is
a full 1080p encode and the box is also serving.

### 2. Playout (`bumparr/station/playout.py`)

**Purpose.** Run a channel as a virtual clock. No thread, no process: the
timeline advances when someone asks for the playlist.

**State per channel** (`Channel` object, held in a module-level dict):

- `name` (`live` | `standby`), `kinds` filter (None for live, the
  `STANDBY_KINDS` set for standby);
- `epoch`: wall-clock start of the timeline (process start);
- `timeline`: list of `Entry(start, item_id, key, segments, duration)`
  in start order, extended lazily;
- `media_sequence`: monotonically increasing count of segments that have
  fallen out of the window;
- `reported_upto`: index of the last entry whose start has been written to
  the database.

**Advance.** `advance(now)` extends the timeline until it covers
`now + lookahead` (lookahead = `STATION_WINDOW_SEGMENTS × STATION_SEGMENT_SECONDS`,
default 6 × 4 = 24 s), choosing each next item by:

1. pool = conformed items eligible for this channel (from the conform
   index, joined with the registry for `weight`, `kind`, `last_played`,
   `play_count`);
2. factors = `seasons.factors_now()` and `dayparts.factors_now()`;
3. weights = `rotation.weights_for(pool, season_factors, daypart_factors)`
   (see §4 for the signature change);
4. pick one by weighted random with the one rule the rotation model does
   not encode: **never the same item twice in a row**.

If the pool is empty the channel airs a single built-in conformed slate:
black with the brand centred, 10 s, drawn with `render_cards` primitives,
encoded through `ffmpeg_pipe`, then conformed like any item into the key
`slate`, so the stream never 404s once ffmpeg has run. The slate is the only
thing Bumparr synthesises for the station; it is not registered as a
playable.

Entries older than `now − 2 × lookahead` are dropped from the front and
their segment counts added to `media_sequence`.

**Reporting.** When `advance` passes an entry's `start`, it writes:
`INSERT INTO play_history(channel_id, playable_id, played_at)`,
`UPDATE playables SET last_played=?, play_count=play_count+1 WHERE id=?`,
and upserts `playout(channel_id, current_id, started_at)`. `channel_id` is
`station:live` / `station:standby`. Reporting is done at most once per
entry and only for entries whose start is in the past, so a playlist
request never counts something that has not aired.

**Playlist.** `playlist(now)` renders:

```
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:<ceil(max segment)>
#EXT-X-MEDIA-SEQUENCE:<media_sequence>
#EXTINF:4.000,
/station/seg/<key>/000.ts
…
#EXT-X-DISCONTINUITY   ← between entries
```

The window covers segments from the one containing `now − 1 segment`
through `STATION_WINDOW_SEGMENTS` ahead. No `#EXT-X-ENDLIST`: it is live.
Segment URLs are absolute using the same `_absolutize` rule as `/playlist.m3u`
(`PUBLIC_URL` or the request), because Dispatcharr's proxy fetches segments
from a different process than the one that read the playlist.

**Determinism.** `advance`, `playlist`, and reporting take `now` as a
parameter and the random source is injectable, so tests drive the clock
and assert exact playlists.

### 3. Routes (`bumparr/station/routes.py`, an `APIRouter` mounted in `app.py`)

| Route | Returns |
|---|---|
| `GET /station/{channel}/index.m3u8` | the live playlist, `application/vnd.apple.mpegurl`, `Cache-Control: no-store`; 404 for an unknown channel |
| `GET /station/seg/{key}/{n}.ts` | `StaticFiles` mount on `ASSET_ROOT/.cache/station`, `video/MP2T` |
| `GET /station/channel.m3u` | two `#EXTINF` lines with `tvg-id="bumparr.live"` / `tvg-id="bumparr.standby"`, `tvg-name`, `group-title="Bumparr"`, absolute URLs |
| `GET /station/guide.xml` | XMLTV, see §5 |
| `GET /api/station` | JSON: per channel `now` (id, title, kind, started_at, ends_at), `next` (same shape), `conformed`, `eligible`, `pending`, and the three URLs |
| `POST /api/station/conform` | starts a conform pass as a background job; returns the job id |

The segment mount has `check_dir=False` like the produced-media mount, so the
app boots before the cache exists.

### 4. Dayparts (`bumparr/dayparts.py`, `bumparr/config_files/dayparts.yaml`)

**Purpose.** Time-of-day character, as a rotation factor and as guide blocks.

**YAML.** Local time (`TZ`), 24-hour, windows may wrap midnight:

```yaml
# A window names a block of the day and says which kinds belong to it.
# Multipliers stack with season and the play-history factors; a kind not
# listed under a window is 1.0 there. A window with `description` becomes a
# guide programme with that text.
overnight:
  hours: "00:00-06:00"
  description: "Windows, dead air, and the occasional unexplained number."
  kinds: {window: 2.0, dead_air: 2.0, number: 1.5, trivia: 0.4, psa: 0.5}
morning:
  hours: "06:00-10:00"
  description: "Weather, the time, and the day's on-this-day."
  kinds: {weather: 3.0, local_time: 2.5, on_this_day: 2.0, dead_air: 0.2}
daytime:
  hours: "10:00-18:00"
  description: "The full rotation."
evening:
  hours: "18:00-24:00"
  description: "Trivia, tiny games, and the odd correction."
  kinds: {trivia: 2.0, tiny_games: 2.0, corrections: 1.5, dead_air: 0.3}
```

Ships with this US-centric default and the same "these are suggestions"
header as `seasons.yaml`. A missing or unparseable file means no dayparts:
every factor 1.0, guide blocks brand-titled. Overlapping windows are a
validation error at load (logged, file ignored) rather than a silent
ordering rule.

**API.** `load_dayparts(path)`, `current(now)` → the window dict or None,
`factors_now(now)` → `{kind: multiplier}`, `blocks(start, end)` → list of
`(start, end, name, description)` covering the range with window boundaries,
brand-titled gaps filled in.

**Rotation.** `rotation.score` becomes
`base × season × daypart × recency × affinity × fatigue`; `build_context`
and `weights_for` take `daypart_factors=None` as a third argument and
`explain` reports the new factor. `/api/bumpers/random` passes
`dayparts.factors_now()` alongside `seasons.factors_now()`. `/fill` is
unchanged. ROTATION.md's table and formula gain the row.

### 5. Guide (`bumparr/station/guide.py`)

XMLTV 1.0. Two `<channel>` elements (`bumparr.live`, `bumparr.standby`)
with `<display-name>` from `BRAND`. Programmes cover `now − 6 h` to
`now + 24 h`:

- `live`: one `<programme>` per daypart block from `dayparts.blocks()`, title
  `"<BRAND> — <block name>"`, `<desc>` from the YAML; brand-titled hourly
  blocks where no daypart applies.
- `standby`: one rolling programme per 6 h, title `"<BRAND> — Please stand by"`,
  desc "Standby loop: station IDs, test cards, and live windows."

Times are `YYYYMMDDHHMMSS ±HHMM` in the process's local zone. The
response is `application/xml`, `Cache-Control: max-age=300`.

### 6. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `STATION_SEGMENT_SECONDS` | `4` | segment length; also the keyframe cadence divisor |
| `STATION_WINDOW_SEGMENTS` | `6` | segments in the live window; lookahead = window × segment |
| `STATION_CONFORM_INTERVAL` | `300` | seconds between conform sweeps |
| `STATION_CONFORM_TIMEOUT` | `600` | per-item ffmpeg ceiling |
| `STANDBY_KINDS` | `technical_difficulties,station_id,dead_air,window` | kinds the standby channel may air |
| `STATION_BITRATE_K` | `4000` | conform target video bitrate |

All documented in CONFIG.md and `.env.example`, per the contributing rule.

### 7. Dashboard

One new panel, "Station", between the pool summary and the actions:

- for each channel: now playing (title, kind, seconds remaining) and next up;
- the three URLs (`channel.m3u`, `guide.xml`, `standby/index.m3u8`) as
  read-only inputs with a copy button;
- conform status `conformed / eligible` with a "Conform now" button wired to
  `POST /api/station/conform` through the existing job-polling path.

Built with DOM APIs (no HTML strings), refreshed on the existing 20 s tick,
covered by `app.test.js` with the same fake-DOM fixture. No new CSS
concepts beyond the existing `.panel`/`.mini`.

### 8. Integration docs

INTEGRATION.md gets a rewritten Dispatcharr section:

1. add `http://bumparr:8780/station/channel.m3u` as an M3U source and
   `http://bumparr:8780/station/guide.xml` as an EPG source; the `tvg-id`s
   match so the guide auto-assigns;
2. for failover, add `http://bumparr:8780/station/standby/index.m3u8` as the
   **last** stream on any channel; when the provider dies Dispatcharr rotates
   onto the standby loop;
3. `PUBLIC_URL` must be reachable from Dispatcharr's container.

ErsatzTV and Tunarr sections each gain a paragraph on adding the live
channel as a stream source. README's "Consume it" table gains the station
row. ARCHITECTURE.md gains the station in the content-flow diagram and one
invariant: **the station airs only conformed items, and conform never runs
in the request path.** CHANGELOG gets a "Station" section. API.md documents
every route above.

## Error handling

- Conform failure for one item: logged with the stderr tail, the item is
  skipped, the sweep continues; a `.part` directory never lands. Retried on
  the next sweep with no backoff beyond the interval (the interval is the
  backoff).
- Missing ffmpeg: conform logs once per sweep and does nothing. With nothing
  conformed, not even the slate, `index.m3u8` returns 503 with a plain-text
  reason; `/api/station` reports `ffmpeg: false`.
- Segment file vanished under a live playlist (cache pruned mid-window): the
  playlist still lists it; the player gets a 404 for one segment and
  recovers at the next. The sweep never prunes a key that appears in any
  channel's current window, which makes this a non-event in practice.
- Registry row disappears while its entry is on the timeline: the entry
  still airs (the segments exist); reporting becomes a no-op for a missing
  id.
- Empty pool: the slate loops, and `/api/station` says so.
- Unknown channel name: 404.

## Testing

All under `tests/`, `unittest`, no ffmpeg in CI:

- `test_station_conform.py`: command construction for audio/no-audio/image
  sources (mocked `subprocess`), key derivation, atomic landing, staleness
  detection, orphan pruning, per-item failure isolation, timeout kill/reap.
- `test_station_playout.py`: injected clock and RNG; timeline extension;
  never-twice-in-a-row; window and media-sequence arithmetic across entry
  boundaries; discontinuity placement; reporting exactly once and only for
  started entries; empty-pool slate; standby kind filtering; pruning of old
  entries.
- `test_dayparts.py`: parsing, wrap-around windows, overlap rejection,
  `factors_now` at boundaries, `blocks()` filling gaps, missing-file
  behaviour.
- `test_rotation.py` additions: the daypart factor in `score`/`explain`,
  default 1.0 when absent.
- `test_station_guide.py`: parse the XML; channels present; programmes
  contiguous over the range; times carry the offset.
- `test_station_routes.py` (FastAPI TestClient; `httpx` joins
  `requirements-dev.txt` for this, CI already installs that file): playlist
  headers and body, 404s, 503 when nothing is conformed, absolute URLs under
  `PUBLIC_URL`, `channel.m3u` shape, `/api/station` shape, conform job start.
- `app.test.js` additions: station panel renders now/next and URLs from a
  fake `/api/station` payload with hostile strings.

The plan's final task is the live check the spike could not do locally:
point a real Dispatcharr at a running Bumparr and confirm the channel plays
and fails over. That task produces a written result in the plan, not code.

URL helpers: `_public_base`/`_absolutize` move from `app.py` into a small
`bumparr/urls.py` so the station router can build absolute URLs without a
circular import; `app.py` keeps using them under the same names.
