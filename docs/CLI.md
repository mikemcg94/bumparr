# CLI reference

Every heavy job is a `python -m` module. The app runs several of them as
subprocesses (API actions, the background jobs in `jobs.py`), so the CLI is
not a back door — it is the same code the service uses, just invocable by hand.

Run inside the container:

```bash
docker compose exec bumparr python -m bumparr.<module> [flags]
```

All modules call `db.init_db()` first, so they work on a fresh database.
Anything that needs the network or ffmpeg needs the container's environment
(keys, `ASSET_ROOT`, etc.), which is why `exec` is the right way in.

## Content production

### `bumparr.produce` — quarry source video into branded clips

Cuts scene-aligned, overlapping, length-banded windows out of everything under
`VIDEOS`, brands them with the slam, resolves audio, registers the results.

| Flag | Meaning |
|---|---|
| `--category NAME` | only this source sub-directory |
| `--limit N` | max source files |
| `--per-source N` | override clips per source |
| `--seed N` | deterministic rolls |
| `--delete-source` | remove each source after its clips are written. **Irreversible.** |

### `bumparr.station_ids` — combinatorial station IDs

Crosses mounted stills/clips (and black) with independently-rolled font
roulettes; lengths spread from a 0.2s flash to a 12s ident (the short
denominations the fill contract needs).

| Flag | Meaning |
|---|---|
| `--count N` | how many (default 60) |
| `--seed N` | deterministic rolls |
| `--dry-run` | plan without rendering |

### `bumparr.render_cards` — text cards → MP4

Renders unrendered cards (volatile kinds re-check their TTL), points their
rows at the files. Offline and idempotent. Details in [RENDERING.md](RENDERING.md).

| Flag | Meaning |
|---|---|
| `--limit N` | render at most N this pass |
| `--kind KIND` | restrict (repeatable) |
| `--force` | re-render even if a file exists |
| `--refresh-volatile` | only the perishable kinds (clock, weather) whose files expired |

### `bumparr.starter` — run the shipped starter seeds

| Flag | Meaning |
|---|---|
| `--dry-run` | list what would be pulled |
| `--limit N` | only the first N seeds |
| `--only-free` | skip stock-API entries (archive.org needs no key) |

## Card generators

| Module | What it does | Flags |
|---|---|---|
| `bumparr.generators.grounded` | factual cards from real sources: `--kind trivia` (Open Trivia DB), `--kind fun_facts` (Wikipedia), `--kind number` (vendored dataset) | `--kind`, `--n` (default 25) |
| `bumparr.generators.cards` | model-invented kinds: `psa`, `corrections`, `achievements`, `coming_up`, `tiny_games`. Needs `LLM_BASE`/`LLM_MODEL` | `--kind`, `--n` (default 20) |
| `bumparr.generators.on_this_day` | Wikipedia on-this-day, tone-filtered, date-bound (self-rotating) | `--n` (default 20) |
| `bumparr.generators.weather` | live conditions card for a place (Open-Meteo). One card per location, upserted | `--location "City, Region"` (defaults to `HOME_LOCATION`) |
| `bumparr.generators.enrich_bg` | attach CC0/public-domain backgrounds (Openverse) with source/license metadata; idempotent, grim-aware, 10-min deadline | none |

The API's `POST /api/generate/{kind}` routes to these same modules.

## Maintenance

### `bumparr.prune` — remove off-shape material

| Flag | Meaning |
|---|---|
| (default) | **dry run**: report portrait videos by kind + orphans, delete nothing |
| `--apply` | actually delete. **Irreversible.** |
| `--skip-orphans` | leave unregistered portrait files alone |
| `--drop-category NAME` | remove a whole category — registry rows, their files, **and any unregistered files sitting in the category directory** (repeatable). The fix for a search that returned junk. The dry run lists every file by name; read it before `--apply`. |

### `bumparr.seasons` — seasonal weighting

| Flag | Meaning |
|---|---|
| (default) | report-only: each gated category's current factor and state, no writes |
| `--apply` | actually write the gated weights (default only reports). **Irreversible.** |
| `--preview` | show every category's weight curve across the year |
| `--date YYYY-MM-DD` | pretend it is this date |

> [!NOTE]
> Selection-time scoring uses `seasons.factors_now()` (computed, never stored);
> the in-place `apply()` path is retained for reporting/healing — see
[ROTATION.md](ROTATION.md).

## Self-maintaining sources

| Module | What it does | Flags |
|---|---|---|
| `bumparr.sources.capture_windows` | snapshot the YouTube-backed live cams (from `live_cams.yaml`'s `snapshot_cams`) into fresh looping MP4s; each cam overwrites its own file | none. Env: `WINDOWS_DIR`; database is always `DB_PATH` |
| `bumparr.sources.fetch_queue` | one pass over the self-healing archive.org queue (`fetch_queue.yaml`); failures stay queued for the next pass | none. Env: `DATA_DIR` (state file) |

`capture_windows` and `fetch_queue` already run on the background-job schedule
(`WINDOW_REFRESH_HOURS`); the API actions `POST /api/sources/capture-windows`
and `POST /api/sources/fetch-queue` run them on demand. The obsolete
`resolve_cams` command was removed; configured HLS cams and scheduled snapshot
captures are the maintained paths.

## Batch script

### `bumparr/tools/overnight.sh`

The scheduled full pass: generate cards across all kinds → enrich eligible
cards with CC0/public-domain backgrounds → render every
unrendered card → quarry the source pool (`--per-source 5`, no
`--delete-source`) → print final counts. **Nothing is deleted** — every phase
only adds, so a bad pass costs disk and nothing else.

- Single-instance lockfile guard (`/assets/.cache/overnight.lock`): two racing
  runs writing the same SQLite file lose cards.
- Logs to `/assets/.cache/overnight.log`.
- Expects the service environment (run it via `docker compose exec`), and
  reads `DB_PATH` for the final count.

Schedule it however you like (host cron, calendar, or manually); Bumparr
intentionally ships no host-side scheduler. A typical entry:

```bash
30 3 * * * docker compose -f /path/to/docker-compose.yml exec -T bumparr bash /app/bumparr/tools/overnight.sh
```
