# Station

## Contents

- [What the station is](#what-the-station-is)
- [The two channels](#the-two-channels)
- [Conform](#conform)
- [Playout](#playout)
- [Dayparts](#dayparts)
- [The guide](#the-guide)
- [Endpoints](#endpoints)
- [Settings](#settings)
- [Operating it](#operating-it)

## What the station is

Dispatcharr relays live streams and has no file playback or scheduler. Its
plugins are action buttons, not playback hooks, so an M3U of individual bumper
files becomes a list of dead channels. The first-class integration is for
Bumparr to be a stream: the pool is served as a continuous HLS channel.

The second channel is the important operational piece. Dispatcharr can put
the standby stream last in a channel's failover list. When the provider dies,
the channel falls back to Bumparr's branded standby loop instead of showing a
spinner. ErsatzTV, Tunarr, and other HLS players can consume the live channel
directly as well.

## The two channels

`live` draws from every eligible, conformed playable item. `standby` uses the
same playout engine but restricts its pool to the kinds in `STANDBY_KINDS`.
The default is `technical_difficulties,station_id,dead_air,window`; window
captures are included, but `stream` rows are not.

## Conform

Conform makes each item splice-safe once, outside the request path. The
eligibility query is `enabled=1 AND health='ok' AND type IN
('video','card','image') AND uri IS NOT NULL AND uri!='' AND duration>0`.
Images are rendered as stills for their duration. Stream rows are never
conformed.

The output profile is fixed: `1920x1080`, 30 fps, H.264 High 4.1,
`yuv420p`; `libx264` with the `veryfast` preset; `-g 60`, `-keyint_min 60`,
and `-sc_threshold 0`; target video bitrate `STATION_BITRATE_K` (4000k by
default), maxrate 1.125 times that target (4500k by default), and a buffer of
twice the target (8000k by default). Video uses
`scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p`:
it letterboxes or pillarboxes and never crops or stretches. Audio is AAC at
48 kHz stereo and 128k. An input with audio uses
`aresample=async=1,apad`; an input without audio gets
`anullsrc=r=48000:cl=stereo`.

The container is MPEG-TS, cut by ffmpeg's segment muxer into
`STATION_SEGMENT_SECONDS` segments (4 seconds by default). Every segment
starts on a keyframe. The cache is under
`ASSET_ROOT/.cache/station/<key>/`, with `000.ts`, `001.ts`, and so on plus
`index.json`. The key hashes the row id, source `(mtime, size)`, and a versioned
description of the complete output profile. The profile includes segment
duration and bitrate; image keys also include the requested still duration.
Changing an output-affecting setting therefore creates a replacement instead
of silently reusing incompatible persistent segments. An index has this shape:

```json
{"id": "card:trivia:…", "key": "…", "source_mtime_ns": 1725500000000000000,
 "source_size": 123456, "segments": [4.0, 4.0, 2.07], "duration": 10.07,
 "conformed_at": 1725500123.4}
```

Staleness is determined by both source identity and conform profile. A change
produces a new key; the old directory is removed only after the replacement
actually lands. A limited pass, failed encode, or source change during a pass
keeps the prior usable rendition. The sweep also removes directories for
deleted, disabled, or parked rows when no live timeline retains them. It is
safe to repeat. Work is written to `<key>.part/` and renamed into place.

`conform.sweep()` runs oldest-first, and `station_conform_loop` runs it at
startup and every `STATION_CONFORM_INTERVAL` seconds. A module-level lock
allows one conform pass at a time. A failed item is logged and skipped; a
timeout kills and reaps ffmpeg and removes the partial directory.

When the eligible pool is empty, the station can use one built-in ten-second
black brand slate. It is drawn with the card-rendering primitives, encoded
through the shared frame pipe, and stored with logical id `slate` under an
immutable key derived from its branding/render fingerprint. Brand, brand-font,
font-file, profile, or render-input changes land a new slate without mutating
segment URLs already on air; failed refreshes preserve the previous slate. It
is not a registered playable. If ffmpeg is unavailable, the slate cannot be made.
With no conformed item, the HLS endpoint returns `503` until something is
conformed.

## Playout

Playout is a virtual clock, not a continuously running encoder. A channel's
timeline is extended when a playlist is requested, using a lookahead of
`STATION_WINDOW_SEGMENTS × STATION_SEGMENT_SECONDS` (6 × 4 = 24 seconds by
default). It chooses from conformed eligible items using seasons, dayparts,
and the normal rotation weights. A zero or negative score is a hard exclusion,
including a seasonal `off_weight: 0`. The same item is not selected twice in a
row when another positive-score item exists; if it is the only eligible item,
repeating it is preferable to airing gated content. The slate is used when no
positive-score playable exists.

When an entry's start time passes, playout writes one `play_history` row,
updates `last_played` and increments `play_count`, and upserts the channel's
`playout` cursor. It never changes `weight`, and the slate is never recorded.
If nobody requests a playlist for longer than the window, measured from the
last playlist request rather than the precomputed timeline end, the stale
timeline is discarded and restarted at the current time before reporting;
otherwise unwatched history would be recorded as aired. Status snapshots only
inspect an existing timeline: they never extend it or write play history.
`stream` rows are never aired by this engine.

## Dayparts

The shipped configuration is
`bumparr/config_files/dayparts.yaml`. It uses local time from `TZ`, 24-hour
`HH:MM-HH:MM` windows, and half-open intervals: the start belongs to the
window and the end does not. A window may wrap midnight, such as
`22:00-06:00`; it is treated as two intervals. Overlapping windows are a
validation error: the file is logged and ignored rather than ordered
silently. A missing or invalid file means no dayparts, factors of 1.0, and
brand-titled guide blocks.

Each window's `kinds` map is a rotation multiplier. An unlisted kind is 1.0;
the multiplier stacks with season and play-history factors and is never
written to the database. The same windows become guide blocks. Gaps are filled
with brand-titled blocks, and a window's `description` is shown to viewers.

## The guide

`/station/guide.xml` returns XMLTV 1.0 for channel ids `bumparr.live` and
`bumparr.standby`. It covers six hours before the current time through 24
hours after it. The live channel has one programme per daypart block, titled
`<BRAND> — <block name>`, with the YAML description; gaps are hourly,
brand-titled blocks. Standby has one rolling programme per six hours titled
`<BRAND> — Please stand by`, with the description `Standby loop: station IDs,
test cards, and live windows.` Times use the process's local-zone offset.

## Endpoints

| Endpoint | Behaviour |
|---|---|
| `GET /station/{channel}/index.m3u8` | Live HLS playlist; `application/vnd.apple.mpegurl`, `Cache-Control: no-store`; 404 for an unknown channel. |
| `GET /station/seg/{key}/{n}.ts` | Static conformed segment from `ASSET_ROOT/.cache/station`, served as `video/mp2t`. |
| `GET /station/channel.m3u` | M3U with live and standby `#EXTINF` lines, `tvg-id` values `bumparr.live` and `bumparr.standby`, `tvg-name`, `group-title="Bumparr"`, and absolute URLs. |
| `GET /station/guide.xml` | XMLTV guide described above. |
| `GET /api/station` | Per-channel `now` and `next` objects (`id`, `title`, `kind`, `started_at`, `ends_at`), plus `conformed`, `eligible`, `pending`, and the three station URLs. |
| `POST /api/station/conform` | Starts a conform pass as a background job and returns its job id. |

The `now`/`next` object is null when there is no corresponding entry. Reading
station status does not create or advance a channel timeline. HLS
playlists use absolute segment URLs based on `PUBLIC_URL` or the request; the
segment mount uses `check_dir=False`, so the app can start before the cache
exists.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `STATION_SEGMENT_SECONDS` | `4` | HLS segment length and keyframe cadence divisor. |
| `STATION_WINDOW_SEGMENTS` | `6` | Segments in the live window; lookahead is window × segment. |
| `STATION_CONFORM_INTERVAL` | `300` | Seconds between conform sweeps; `0` disables the loop, but not the API action. |
| `STATION_CONFORM_TIMEOUT` | `600` | Per-item ffmpeg ceiling while conforming. |
| `STATION_BITRATE_K` | `4000` | Target video bitrate in kbit/s for the conformed profile. |
| `STANDBY_KINDS` | `technical_difficulties,station_id,dead_air,window` | Comma-separated kinds allowed on standby. |

## Operating it

On the first run, expect conform to take a while: it is a full 1080p encode
per eligible item. Start a pass with `POST /api/station/conform`, or use the
dashboard's Station panel and its **Conform now** action. The panel shows
conformed versus eligible progress and the station URLs.

Set `PUBLIC_URL` to a URL reachable from the consumer, especially when
Dispatcharr is in another container or behind a reverse proxy. Conforming is
one encode at a time, using ffmpeg's `veryfast` preset, so it shares the box's
CPU with the service.

Plan for a second copy of the pool on disk: conformed segments are stored in
addition to source media. At the target video bitrate, the rough video-only
estimate is `items × duration_seconds × STATION_BITRATE_K / 8` kilobytes
(add audio and MPEG-TS overhead; with the default 4000 kbit/s this is about
0.5 MB per second per item).

For a `503`, check that ffmpeg is installed and that a sweep has produced at
least one cache index; the API reports `ffmpeg: false` when ffmpeg is missing.
For an empty guide grid, check that `/station/guide.xml` is reachable and that
the consumer is using the matching `tvg-id` values. If a player stutters at
item boundaries, it must honor `#EXT-X-DISCONTINUITY`; if a segment vanishes,
the specified fallback is recovery on the next segment while the sweep keeps
live-window keys from pruning.
