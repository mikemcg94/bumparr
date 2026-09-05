# Configuration reference

Every Bumparr setting is a plain environment variable with a working default:
unset = the default in the last column, and the service runs. Copy
`.env.example` to `.env` for the commonly-touched ones; everything else can be
passed via the environment (or the `environment:` block in `docker-compose.yml`)
whenever you want to tune it.

Variables are function-named, no prefix, no product name in a variable.

## Server

| Variable | Default | Effect |
|---|---|---|
| `PORT` | `8780` | Published port (compose-level; the container itself always listens on 8780). |
| `PUBLIC_URL` | derived from the request | Base URL in every URL Bumparr hands out (M3U entries, `media_url`). **Set this behind a reverse proxy** or on a non-default hostname; otherwise Bumparr mirrors whoever asked, which is silently wrong when the fetcher and the player are different machines (it warns once if it detects loopback). |
| `TZ` | host time | IANA timezone for the clock card (`America/New_York`). Containers are UTC by default, which is why this is explicit. |

## Station

| Variable | Default | Meaning |
|---|---|---|
| `STATION_SEGMENT_SECONDS` | `4` | HLS segment length for the live channels. |
| `STATION_WINDOW_SEGMENTS` | `6` | Segments in the live window; lookahead = window × segment. |
| `STATION_CONFORM_INTERVAL` | `300` | Seconds between conform sweeps. `0` disables the loop (the API action still works). |
| `STATION_CONFORM_TIMEOUT` | `600` | Per-item ffmpeg ceiling when conforming. |
| `STATION_BITRATE_K` | `4000` | Target video bitrate (kbit/s) of the conformed profile. |
| `STANDBY_KINDS` | `technical_difficulties,station_id,dead_air,window` | Kinds the standby channel may air. |

## Storage

| Variable | Default | Effect |
|---|---|---|
| `ASSET_ROOT` | `/assets` | Where bumper media and the fetch queue's output live; served under `/media`. Source material is organized by category sub-directory. |
| `DB_PATH` | `<repo>/data/bumparr.db` | The SQLite database (registry + playout + history). |
| `VIDEOS` | `ASSET_ROOT` | Where the **quarry** looks for source video (produce scans this). |
| `IMAGES` | `ASSET_ROOT` | Where stills for station-ID plates are looked up. |
| `SOUNDS` | `ASSET_ROOT/music_beds` | Music beds for silent produced clips. Empty = silent clips stay silent. |
| `OUTPUT` | `ASSET_ROOT/bumpers` | Where finished produced clips land (served under `/media/bumpers`). |
| `DATA_DIR` | `/data` | Working state for the fetch queue (`fetch_done.json`). CLI-module-level. |
| `WINDOWS_DIR` | `ASSET_ROOT/windows` | Where live-window snippets are written. Ephemeral: never quarried by produce. |
| `FRONTEND` | the `bumparr/` package dir | Secondary font search path (for co-deployed players). |

## Local model (optional)

Bumparr ships no model. Grounded, procedural, and starter-seed cards all work
with none of these set; the model only diversifies the model-generated kinds.

| Variable | Default | Effect |
|---|---|---|
| `LLM_BASE` | empty | Any OpenAI-compatible chat endpoint (`http://host:port/v1`). Empty = model features off. |
| `LLM_MODEL` | empty | Model name/id to pass in the request. |
| `LLM_DISABLE_THINKING` | empty | `1`/`true`/`yes` for reasoning models (Qwen etc.): sends `chat_template_kwargs.enable_thinking=false`, otherwise they burn the whole token budget "thinking" and return empty content. Opt-in because some providers reject unknown fields. |

## Stock-footage keys (for starter seeds and "more X" requests)

| Variable | Default | Effect |
|---|---|---|
| `PEXELS_API_KEY` | empty | Pexels video search (free self-signup). |
| `PIXABAY_API_KEY` | empty | Pixabay video search (free self-signup). |

Both optional; archive.org and Wikimedia Commons need no key.

## Rotation / scheduler tuning

The scoring model itself is documented in [ROTATION.md](ROTATION.md). These are
its curve parameters; leave them alone until you have a specific variety
complaint, and change one at a time.

| Variable | Default | Effect |
|---|---|---|
| `RECENCY_TAU` | `10800` (3h) | How long a clip takes to "recover" after airing. At `tau` it is at 50% pull. Raise for more spacing between repeats. |
| `RECENCY_FLOOR` | `0.015` | Pull of a clip that just aired (near-zero = effectively out of the running). |
| `AFFINITY_TAU` | `1500` (25m) | Same: for a whole *category*. Two traffic cams back to back is a texture problem, so this recovers faster than recency. |
| `AFFINITY_FLOOR` | `0.30` | Category pull right after one of its own aired. |
| `FATIGUE_STRENGTH` | `0.5` | Exponent on the lifetime-overplay penalty (relative to the pool's median plays). |
| `FATIGUE_MIN` / `FATIGUE_MAX` | `0.25` / `1.6` | Clamp on the fatigue factor, so an overplayed item is discounted, never silenced (or boosted). |
| `COOLDOWN` | `4` | Legacy floor for repeat protection; the share-based knobs below do the real work. |
| `COOLDOWN_FRACTION` | `0.35` | Fraction of the pool that counts as "recently played". Scales with pool size. |
| `COOLDOWN_MAX` | `400` | Cap on the recent set so a huge pool always keeps candidates available. |
| `RECENCY_HORIZON` | `21600` (6h) | Beyond this age, "freshness" stops giving a clip any further advantage. |
| `RECENCY_MAX_BOOST` | `6.0` | Maximum advantage the freshness signal can confer. |

## Durations and shape policy

| Variable | Default | Effect |
|---|---|---|
| `CARD_DUR` | `14` | Seconds a text card stays on screen. |
| `STREAM_DUR` | `45` | Seconds a live "window" stream is forced off after. |
| `VIDEO_MAX` | `75` | Hard cap: no video overstays its welcome, however long the file is. |
| `MIN_VIDEO_ASPECT` | `0.95` | Videos narrower than this are phone-format and pruned; portrait *stills* are exempt. |

## Brand, fonts, and the slam

| Variable | Default | Effect |
|---|---|---|
| `BRAND` | `TV` | The one value every brand surface reads: the slam, station IDs, PSAs, the player logo. Rebrand by setting only this. |
| `BRAND_FONT` | decorative fallback | Typeface for the brand mark. Filename on the font path, or empty. |
| `CARD_FONT` | `card-inter.woff2` | Body font for rendered cards (the renderer resolves a matching `.ttf`/`.otf`; see `bumparr/fonts/README.md`). |
| `FONTS` | bundled `bumparr/fonts/` | Extra typefaces for the roulette. More faces = a better slot machine. |
| `ROULETTE_MIN_FONTS` | `4` | Below this many faces a clip gets a static slam instead of a roll (two fonts flickering reads as a fault). |
| `ROULETTE_PROB` | `0.43` | Fraction of produced clips whose slam *rolls*; the rest sit static, by design. |

## Background jobs

| Variable | Default | Effect |
|---|---|---|
| `WINDOW_REFRESH_HOURS` | `2` | How often live-window snippets are re-captured. |
| `WINDOW_REFRESH` | `1` | `0`/`false`/`no` disables the window refresh loop entirely (capture elsewhere, or you have no snapshot cams). |
| `VOLATILE_INTERVAL` | `60` | Seconds between checks for perishable cards (clock, weather) whose rendered file has expired. |
| `TTL_LOCAL_TIME` | `60` | Seconds a rendered clock card stays truthful before re-render. |
| `TTL_WEATHER` | `1800` | Seconds a rendered weather card stays truthful before re-render. |

## Production (produce.py)

| Variable | Default | Effect |
|---|---|---|
| `SCENE_THRESHOLD` | `0.2` | ffmpeg scene-detection threshold for finding shot boundaries to cut on. |
| `SILENCE_DB` | `-40` | Mean volume below which a window counts as silent (eligible for a music bed). |
| `ADD_SOUND_MIN` / `ADD_SOUND_MAX` | `0.40` / `0.75` | Random range for the share of silent clips that receive a bed. The rest stay silent on purpose. |
| `EPHEMERAL_DIRS` | `windows` | Comma list of source dirs holding live captures — never quarried into permanent clips. |
| `OUTPUT_DIRS` | `cards,bumpers` | Comma list of dirs holding Bumparr's own finished output — never quarried. |

## Ingest

| Variable | Default | Effect |
|---|---|---|
| `HOME_LOCATION` | empty | "weather at home" resolves here (e.g. `Portland, Oregon`). Empty = weather requests must name a place. |
| `MAX_DOWNLOAD_MB` | `500` | Actual-byte cap for one direct ingest download. |
| `STREAM_SEGMENT_MAX_MB` | `64` | Actual-byte cap for one proxied HLS segment; playlists are fixed at 2 MiB. |
| `ALLOW_PRIVATE_UPSTREAM` | off | Permit configured HLS/background-image hosts to resolve to private or special-use IPs. Enable only for deliberate LAN feeds. The legacy `ALLOW_LOOPBACK_UPSTREAM` name is accepted temporarily. |

## Compose-level (docker-compose.yml)

| Variable | Default | Effect |
|---|---|---|
| `ASSETS` | `bumparr-assets` | Named volume mounted as `/assets`; set a host path to use a UID/GID 10001-writable bind mount. |
| `DATA` | `bumparr-data` | Named volume mounted as `/data`; set a host path to use a UID/GID 10001-writable bind mount. |
