# Changelog

## Unreleased

Security + correctness pass (plan: [docs/FIX_PLAN.md](docs/FIX_PLAN.md)).

**Station**
- The pool now runs as two live HLS channels, `live` and `standby`, with a
  channel M3U and an XMLTV guide, so Dispatcharr can carry Bumparr as a
  channel and use standby as branded failover. Items are conformed once
  into splice-safe segments by a background job; nothing encodes at serve
  time.
- The playout is the first writer of play history: `last_played`,
  `play_count` and `play_history` now move, which wakes the recency,
  affinity and fatigue factors.
- Dayparts (`config_files/dayparts.yaml`): time-of-day windows as a
  rotation factor and as the guide's programme blocks.
- New settings: `STATION_*`, `STANDBY_KINDS` (see CONFIG.md).
- Conform cache keys now include the active output profile and still duration;
  branding changes invalidate the slate, and old renditions remain available
  until their replacements successfully land.
- Status polling is side-effect free, reconnect staleness follows the last HLS
  playlist request, and zero-score seasonal/daypart candidates stay off air.
- MPEG-TS station segments are served explicitly as `video/mp2t` on every
  supported Python/OS MIME database.

**Security**
- Stream proxy: per-cam signed tokens, same-origin/CDN allowlist with redirect
  validation, and bounded reads (closes a local-file/SSRF primitive).
- Archive fetch + ingest: metadata filenames are sanitized and contained,
  downloads are capped on actual bytes and landed atomically.
- Media deletes resolve through one contained resolver (escapes delete the row
  only); the container runs as non-root with a `/healthz` healthcheck.
- Dashboard builds server-derived content with DOM APIs rather than HTML strings.

**Correctness**
- Contained deletes preserve symlink entries instead of resolving filesystem
  operations onto their targets; staging and archive temporary names remain
  valid even when source metadata reaches filesystem component limits.
- Generic `get`/`fetch` weather requests remain weather-data cards unless an
  explicit media noun or clip count asks for footage; dashboard actions keep
  polling until the server reports a terminal state.
- `/api/bumpers` honors bounded `limit`/`offset`; `/random` returns up to
  `count` (default 5); M3U keeps commas inside quoted titles; `status`
  accumulates `by_kind` across types.
- Trivia auto-labels bare options and rejects mixed labels; weather refresh
  preserves stats/uri/operator tuning; the number baseline is idempotent
  across restarts; `seasons` is report-only without `--apply`.
- `resolve_cams` was removed (snapshot/direct cams in `live_cams.yaml` are the
  maintained path); dashboard search now covers the full registry server-side.

**Recovery**
- `POST /api/pool/revive` now clears a park it can verify: rows the asset sweep
  retired (`enabled=0, health='dead'`) return to rotation when `ffprobe` can
  still read the file. It previously restored `health` alone, and rotation
  requires both, so a parked row stayed dark permanently.
- `POST /api/pool/enable` un-parks one named item — the way back for a live cam
  parked when its entry left `live_cams.yaml`, which has no local file to
  verify. Restoring one is ordered: re-add to the YAML, reload, then enable,
  because the loader parks what is unconfigured but never re-enables what is.
- `/api/bumpers` accepts `?enabled=`, and the dashboard gains a parked-only
  filter and a per-card enable control, so finding a parked item no longer
  means reading ids out of a JSON page.
- Date-rotated cards are excluded from the revive sweep, and enabling one by
  name warns rather than refuses; a single payload matcher now backs the
  rotation, the generator quota, and that warning, so they cannot disagree.
- `prune --drop-category` names every unregistered file it will delete while
  previewing, and compares paths by real directory and entry name so a
  symlinked media root stops reporting registered files twice.

**Ops**
- Default Compose storage now uses writable Docker-managed volumes, so a clean
  checkout starts under the non-root UID without host-directory ownership work.
- CI runs `compileall`, ResourceWarning-strict tests, targeted Ruff, JavaScript
  syntax/DOM tests, and a clean-checkout Compose build/start/write smoke step;
  compose warns the `:ro` dev mount shadows release pins.
- Assumes a trusted LAN: no auth on the dashboard or the POST/DELETE endpoints.

## v0.1.0 — first public release

First release. A bumper generator for the *arr stack: point it at source media
and it builds and maintains a self-refreshing pool of TV bumpers and
interstitials that any channel generator can consume.

**Output**
- `GET /playlist.m3u` — absolute-URL M3U of every playable bumper.
- `GET /api/bumpers/fill?seconds=N` — a *set* of bumpers summing to a gap,
  solved as subset-sum rather than greedy, so a break doesn't end in dead air.
- `GET /api/bumpers/random`, `/api/status`, and per-bumper lookup.

**Bumper kinds**
- Video from your own media and public-domain archives.
- Live "window" cams — open direct-HLS feeds play genuinely live and ship
  enabled. YouTube-backed snapshot cams are supported but ship disabled; a
  commented template in `live_cams.yaml` shows how to add your own.
- Text cards: grounded trivia and fun-facts, verified numbers, plus surreal
  PSAs, fake corrections, achievements and more.
- Procedural station IDs and technical-difficulties cards.

**Works without a model.** Grounded cards come from real sources, procedural
kinds are code, and every model-generated kind ships a built-in starter set —
so a fresh install with no endpoint still has content in every kind. Point
`LLM_BASE` at any OpenAI-compatible endpoint to generate more, in your own
voice. The model diversifies the pool; it is not required.

**Content integrity.** Factual cards are grounded in real sources rather than
invented, and `card_validation` rejects malformed cards at generation time
(unlabelled options with a bare-letter answer, either/or prompts asserting an
answer, truncated facts, placeholder numbers).

**Notes**
- No YouTube entries ship enabled. Whether to scrape YouTube is the operator's
  call, so Bumparr keeps the capability (`yt-dlp` is installed) but leaves it
  switched off by default.
- With no `PUBLIC_URL` set, emitted URLs are derived from the incoming request.
  Bumparr warns if that derivation is a loopback address, since those URLs
  cannot be reached by any other host, container or player.
- Bundled fonts are SIL OFL. No media assets ship with Bumparr.
