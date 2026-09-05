# Bumparr — Fix Plan for the Comprehensive Code Review

Source: the full-spectrum code review (all 29 Python files, `web/`, Docker/CI,
tests, docs, config files). High items were reproduced dynamically in a `/tmp`
sandbox; all others were confirmed by reading the cited code.

Conventions used below:

- **Fix** = concrete code change (file + function + what to do).
- **Test** = regression test to add under `tests/` (unittest, the project's runner).
- **Done when** = acceptance criterion you can verify by hand or in CI.
- Phases are ordered by risk-reduction per effort. Phase 1 is the security
  boundary and introduces shared path/fetch primitives reused later, so it
  should land first.

> Note: line numbers refer to the tree at review time; a parallel
> documentation pass is landing docstrings, so expect small drift.

---

## Phase 1 — Security boundary (do first)

### H1 — SSRF + local-file read in the HLS proxy

**File:** `bumparr/stream_proxy.py:68-103` (`stream_index`, `stream_seg`)

**Problem.** `token` is a bare base64url of an arbitrary URL and is fetched
with no binding to the `pid` row or to a URL emitted by `_rewrite`. The opener
also resolves `file://` (via `build_opener`'s default `FileHandler`), so this is
an unauthenticated local-file disclosure + internal-network fetch primitive.
Both `resp.read()` calls are additionally unbounded (memory DoS).

**Fix.**

1. In `stream_seg`, look up the pid's upstream first via `_upstream(pid)`;
   return 404 when unknown (the same behavior as `stream_index`).
2. Replace the forgeable URL token with a URL plus an HMAC signature bound to
   `pid` (or an equivalently unforgeable opaque token). `_rewrite` is the only
   minting path. Use a process secret created with `secrets.token_bytes()`;
   this deployment runs one uvicorn worker, and invalidating an in-flight HLS
   playlist on restart is acceptable. If multi-worker support is added, move
   the secret to shared configuration before enabling it.
3. Validate every initial and nested URL before minting or fetching it:
   - allow only `http` and `https`, with a non-empty hostname and no embedded
     credentials;
   - default to the configured upstream's exact origin (scheme, normalized
     hostname, and effective port), not hostname alone;
   - because HLS may legitimately use a separate CDN host, support an explicit
     per-cam `proxy_hosts` allowlist in `live_cams.yaml`/the stream payload.
     Do not silently trust arbitrary absolute hosts found in a playlist;
   - apply the same rule to every redirect with a validating redirect handler.
     Validating only the token is insufficient because an allowed URL can
     redirect to loopback, link-local/cloud-metadata, or `file:` targets.
4. Add one bounded read helper used by both `stream_index` and `stream_seg`.
   Check `Content-Length` when present and read at most `limit + 1` actual bytes
   before returning 502 on overflow. Use a small playlist limit (for example
   2 MiB) and an environment-configurable segment limit (for example 64 MiB),
   and close the upstream response on every path.
5. Keep 400 for malformed/invalid/unsigned tokens, 404 for an unknown pid, and
   502 for upstream fetch or size failures. Log the detailed upstream error
   server-side; do not reflect it into the playlist body.

**Test** (`tests/test_stream_proxy.py`): forged token for
`file:///etc/hostname` → 400, never 200; forged or validly encoded foreign
host → 400; allowed same-origin and configured-CDN URLs → proxied; a
redirect to `file:`, loopback, or a foreign origin → blocked; oversized
index and segment bodies → 502 and the response is closed.

**Done when:** the review's repro
(`stream_seg("x", b64("file:///…"))` → 200 + contents) returns 400/404, and
the dashboard can still play both a same-origin stream and a configured
cross-origin-CDN stream end to end.

### H2 — Path-traversal file write via archive.org metadata

**File:** `bumparr/sources/fetch_queue.py:98` (`fetch`)

**Problem.** `dest = outdir/"%s__%s" % (ident, nm)` uses the metadata-supplied
`nm` verbatim. `nm = "../../data/bumparr.db"` escapes the category dir; the
container runs as root, so any container path is writable.

**Fix.**

1. Add shared path helpers (for example `bumparr/paths.py`) rather than copying
   `ingest._download_video`'s partial sanitization. Normalize a filename to one
   basename, replace unsafe characters, strip leading dots, and supply a
   fallback:
   ```python
   # bumparr/paths.py
   def safe_filename(name, default="clip.mp4"):
       fn = name or default
       fn = re.sub(r"[^\w.\-]", "_", os.path.basename(fn)).strip("._") or default
       ...
   ```
   Apply it to the destination forms of both `ident` and `nm`. Validate the
   queue `category` separately as one safe directory component; it is also
   operator YAML and currently permits `../` traversal. URL-quote `ident` and
   `nm` with `safe=""` when constructing archive.org URLs.
2. After joining, assert containment:
   `Path(dest).resolve().is_relative_to(Path(outdir).resolve())`
   (3.12 has `is_relative_to`; else compare `parents`), else skip as failed.
3. Download to a sibling `.part` file, enforce `MAX_MB` on **actual bytes**
   while streaming, then `os.replace()` the destination only after all checks
   pass. On overflow or any exception, remove only the partial; never destroy a
   previously successful destination. Keep the declared-size check as an early
   optimization, not a security boundary.

**Test** (`tests/test_fetch_queue.py`): `pick`/`fetch` with
`nm = "../../evil.mp4"` and `"a/b.mp4"` stays inside `outdir`; categories
`../../outside` and absolute paths are rejected (stub `gj` and `urlopen`);
oversized and mid-stream-failing responses leave no partial and preserve an
existing good file.

**Done when:** hostile metadata names land as flat, sanitized files inside the
category dir, and a lying `size: 1KB` / actual-500MB download is cut off.

### M1 — Traversal-assisted arbitrary file delete

**Files:** `bumparr/app.py:412-496` (`_resolve_media`, `delete_bumper`,
`delete_kind`), `bumparr/prune.py:34-125` (`_resolve`, `drop_categories`)

**Problem.** `Path(ASSET_ROOT)/uri` with an un-normalized DB `uri`; proven to
delete outside the media trees given a crafted row.

**Fix.**

1. Centralize one resolver (replace the local-path logic in `app`, `prune`,
   `revive`, and the later seed sweep with one
   `bumparr.paths.resolve_media(uri)`):
   - `None`/http(s) → `None` (unchanged semantics).
   - `bumpers/…` → under `OUTPUT_DIR`, else under `ASSET_ROOT`.
   - `resolve()` the result and require containment in the corresponding
     root; return `None` (treat as stream/remote: row-only delete) on escape.
2. Add a separate `resolve_category_dir(root, kind)` that accepts exactly one
   safe category component and requires `d != root` plus containment. Use it in
   both `app.delete_kind` **and** `prune.drop_categories`; the media resolver
   does not protect their direct `root/kind` joins. Reuse the same category
   validation in ingest/fetch destinations.
3. Treat an unsafe URI as row-only for single-row deletion, with a server-side
   warning. A bulk category request with an unsafe category is a 400/no-op, not
   a partial delete.

**Test** (`tests/test_paths.py`, `tests/test_app_api.py`):
`resolve_media("../../etc/x")` and absolute/symlink escapes → `None`;
`delete_bumper` on such a row deletes the row but leaves the filesystem
untouched; traversal/absolute `kind` values cannot make `delete_kind` or
`prune.drop_categories` inspect or remove an outside directory.

**Done when:** the review's sentinel repro ends with the file intact.

---

## Phase 2 — Correctness bugs that lose data or loop forever

### M2 — `gen_trivia` infinite network loop on saturation

**File:** `bumparr/generators/grounded.py:57-82`

**Fix.** Copy the bounded-retry shape from `gen_fun_facts`: track requests and
candidate outcomes separately; loop while `added < n` and requests are below a
finite budget such as `max(n * 4, 10)`. Increment the request counter before
each fetch, retain the existing rate-limit sleep, and print a final summary of
added/duplicate/rejected/fetch-error counts when the budget is exhausted. Do
not call every `_insert(False)` a duplicate: validation rejection and duplicate
IDs are different outcomes, so return or measure them separately.

**Test:** stub `_get_json` to return only already-inserted facts; assert the
function returns (does not hang) and adds 0.

### M3 — One malformed card aborts the whole generation batch

**Files:** `bumparr/card_validation.py:96`, `bumparr/generators/cards.py:211-261`

**Fix (both halves).**

1. Validation: reject early in `_validate_trivia` —
   `if len(options) > len(LETTERS): return None, "too many options (max 6)"`.
   This converts the `IndexError` crash into an ordinary rejection.
2. Caller: extract model-object normalization/validation/payload construction
   into a per-item helper and guard that model-derived work with
   `try/except (TypeError, ValueError, KeyError, IndexError)`. Count the item as
   rejected, log a bounded reason, and continue. Keep database operations
   outside that catch so an `sqlite3.OperationalError` or integrity problem
   fails and rolls back the batch instead of being misreported as bad model
   output. Increment `added` only when the INSERT's `rowcount` says it inserted.

**Test:** labelled and unlabelled 7-option objects → `(None, reason)` (no
raise); `generate` with a stubbed model returning one 7-option + one good card
inserts the good one; a simulated database error still propagates/rolls back.

### M4 — Weather refresh wipes stats, uri, and operator tuning

**File:** `bumparr/generators/weather.py:84-93`

**Fix.** Replace `INSERT OR REPLACE` with: try `UPDATE … SET
title/payload/duration … WHERE id=?`; if `rowcount == 0`, `INSERT` the full
row. Never touch `uri`, `play_count`, `last_played`, `enabled`, `health`,
`weight`, `created_at` on refresh. (Refresh of the *rendered file* already
happens via the volatile-TTL re-render path; the row must survive it.)

**Test:** seed a weather row with non-default `uri`, `play_count`,
`last_played`, `enabled`, `health`, `weight`, and `created_at`; run the refresh
with stubbed API data and assert every operator/history/render field is
preserved while title/payload/duration update. Also cover the first insert.

### M5 — Bad live-cam YAML crashes startup; cams can't stay disabled/removed

**File:** `bumparr/live_cams.py:15-57`, caller `bumparr/app.py:72` (`lifespan`)

**Fix.**

1. Validate the document and every item: top level must be a mapping, `cams`
   must be a list, each cam must be a mapping, and `url` must be a non-empty
   `str`. Coerce `weight` with `try/except (ValueError, TypeError)` to `1.0`
   with a warning; normalize `title`/`kind`/`region`; never let one entry abort
   the remaining cams.
2. Stop deriving identity only from URL: that makes “revive when URL changed”
   impossible because a changed URL creates a different row. Add a documented,
   required `id`/`slug` field and derive `stream:cam:<safe-id>` from it. For one
   compatibility release, fall back to the legacy URL hash with a warning;
   document that adding IDs may create new rows and the removal pass parks the
   legacy rows.
3. Preserve operator intent on update: never touch `enabled`; update the
   descriptive/config fields. Reset `health='ok'` only when `uri` changed, so
   an unchanged dead feed stays dead instead of being revived on every boot.
4. Collect configured pids, then park (do not delete; history matters)
   `stream` rows with `source='live-cam'` whose id is no longer configured.
   Handle the empty-list case with a separate `UPDATE` rather than constructing
   `NOT IN ()`. Print the parked count.
5. Belt and braces: wrap `load_cams()` in `lifespan` so an unexpected loader
   bug degrades to “0 cams,” not a dead service.

**Test:** malformed top level, bad weight, non-mapping entry, and non-string URL
do not block good cams; a disabled cam stays disabled; an unchanged dead cam
stays dead; a changed URL revives the same stable ID; removed/all-removed cams
are parked.

### M6 — Background loops die silently on transient errors

**File:** `bumparr/jobs.py:46-54`, `115-153`

**Fix.**

1. `dated_card_loop`: move the two initial `to_thread` calls inside the
   `try` (or wrap each in its own try) so a startup DB lock doesn't kill the
   loop before it starts.
2. `window_refresh_loop`: extract a `_refresh_once(initial=False)` coroutine
   and wrap the initial pass and every periodic pass in
   try/except-log-continue mirroring `dated_card_loop`; only `CancelledError`
   exits. One capture failure must not prevent the fetch-queue pass in the same
   cycle (guard the two operations independently).
3. `_newest_window_age`: move the `max(f.stat()…)` inside the `try`, or use
   a generator with per-file guards; return `None` (="unknown, capture") on
   any `OSError`.

**Test:** stub `_run_capture` to raise once and `_run_fetch_queue` to succeed;
assert the fetch still runs and a subsequent `_refresh_once()` succeeds. Stub
`Path.stat` to raise and assert `_newest_window_age()` returns `None`. Verify
`CancelledError` is re-raised.

### M7 — Unbounded ingest downloads + orphaned partials

**File:** `bumparr/ingest.py:117-149`, `152-178`

**Fix.**

1. Add `MAX_DOWNLOAD_MB` (for example 500 by default, environment-overridable
   and documented) and enforce it on **actual bytes** in `_download_video`.
   Treat `Content-Length` only as an early rejection; read at most one byte
   beyond the cap to detect a lying/absent length.
2. Write `_download_video` to a sibling `.part` and `_capture_youtube` to a
   sibling temporary MP4; validate size/shape there, then atomically
   `os.replace()` the final path. On every error or timeout, remove only the
   temporary file. Do not delete/truncate a previously good destination while
   attempting a refresh.
3. When supplied, reject clearly wrong content types such as `text/html` before
   downloading, but do not require `video/*` because several valid archive/CDN
   endpoints return `application/octet-stream`.

**Test** (`tests/test_downloads.py`): stub `urlopen` with an oversized reader
and one that raises mid-stream; assert bounded abort + no partial. Pre-create a
good destination and verify both failures preserve it. Simulate ffmpeg timeout
and a too-small result for `_capture_youtube`; assert temporary cleanup and
preservation of the prior capture.

### M8 — Startup keeps adding `number` cards until the dataset drains

**File:** `bumparr/generators/grounded.py:126-146`

**Fix.** Separate “install the offline baseline” from “generate more numbers.”
Add `register_number_baseline(n=12)` that selects exactly the same deterministic
first/stably-sampled `n` dataset entries on every boot and attempts only those
entries; duplicate attempts consume the fixed candidate set, so a second boot
adds zero. `register_all_baselines()` calls that helper. Keep `gen_number(n)` as
the explicit expansion path: it may scan past duplicates to add up to `n` new
facts when an operator asks for more. Do not shuffle the loaded list in place;
copy it before any on-demand shuffle.

**Test:** `register_all_baselines()` twice on a temp DB → identical number-row
counts (the review's 12→24→36 repro becomes 12→12); then explicit
`gen_number(5)` can still add up to five facts outside the baseline.

### M9 — Footage requests containing "weather" become weather cards

**File:** `bumparr/ingest.py:612-623` (`handle`)

**Fix.** Narrow the weather-card branch to weather-data intent. Route explicit
footage requests first when they contain a medium noun (`clip(s)`, `footage`,
`video(s)`) or a pull/download verb paired with a count/theme. Only then match
standalone `weather` or `weather in|for|at <place>` as a card. A bare word like
`pull` alone is not enough to classify footage. Add a comment with a case that
actually hits the current bug: `pull 5 weather clips in Seattle` (the original
`pull 5 storm weather clips` example does not enter the current weather branch).

**Test:** `handle("pull 5 weather clips in Seattle")` and
`handle("5 storm-weather videos for my channel")` route to theme search, not
`_weather_card`; `handle("weather in Tokyo")`, `handle("current weather")`,
and `handle("weather at home")` still route to the card (stub network helpers).

### M10 — File-first deletion can leave a live row pointing at no file

**Files:** `bumparr/app.py:422-496`, `bumparr/prune.py:83-185`

**Problem.** The current order unlinks a file before deleting/committing its DB
row. If the SQL operation or commit then fails, the file is already gone and
the surviving row points at nothing. The earlier review incorrectly marked this
as safe; reseeding can repair the *opposite* order's orphaned file, but it cannot
recreate a file that was already unlinked.

**Fix.** Make file/row deletion compensating and recoverable: atomically rename
each target to a unique quarantine name on the same filesystem, execute and
commit the row deletion, then unlink the quarantined file. On SQL/commit failure,
roll back and rename every staged file to its original path. If staging fails,
leave that row unchanged. Apply the same helper to single delete, bulk kind
delete, and prune; only remove empty directories after the transaction succeeds.
Log/return cleanup failures without claiming the item was fully removed.

**Test:** force execute and commit failures after staging and assert the original
file and row both remain; force staging failure and assert the row remains;
successful deletion removes both; a post-commit quarantine-unlink failure is
reported and leaves a recoverable quarantine file, not a live broken row.

---

## Phase 3 — Resource lifecycle + API hardening

1. **Kill leaked ffmpeg on hangs without blocking on stderr first** —
   `station_ids.py:102-104`, `render_cards.py:776-778`: after closing stdin and
   assigning `proc.stdin = None`, call `proc.communicate(timeout=...)` so stderr
   is drained while waiting. On `TimeoutExpired`, `proc.kill()`, call
   `communicate()` again to reap/drain it, remove the incomplete destination,
   and raise with the bounded stderr tail. The current `stderr.read()` occurs
   *before* `wait()` and can block forever, so merely wrapping the later
   `wait()` is not a fix. *Done when:* a wedged ffmpeg neither hangs the caller
   nor accumulates zombies or partial output.
2. **Bound the API subprocess surface** — `app.py:400-705`: validate every
   numeric action parameter with FastAPI `Query`: generation `n` 1–100,
   starter/render `limit` 1–1000 when present, and positive bounded duration
   parameters (`max_duration`/`seconds` up to 86,400 seconds and `tolerance` up
   to 3,600 seconds) on read endpoints. Add a 60s timeout to `revive`'s ffprobe
   and handle `TimeoutExpired` as still-dead.
   Then move starter/render/generate/source actions onto the existing
   background-job mechanism and return job IDs; update the dashboard/API docs
   to poll them. This is part of this phase, not an unspecified “long-term”
   follow-up. Add an in-process concurrency cap so unauthenticated LAN callers
   cannot launch unlimited ffmpeg/download subprocesses.
3. **Close SQLite connections** — `db.py:42-55`: make `conn()` a
   `@contextmanager` that creates the connection, enters `with c:` around the
   yield (preserving sqlite's commit-on-success/rollback-on-error behavior),
   and closes in `finally`. All current `db.conn()` call sites already use
   `with`; verify that remains true with a grep/test. Set persistent
   `journal_mode=WAL` during `init_db()` rather than on every connection, while
   retaining `busy_timeout` per connection. Convert `capture_windows` and
   `enrich_bg` to the shared helper. `resolve_cams` is deleted in Phase 5; until
   those changes land together, its direct connection still needs a `finally`
   close.
4. **Single DB path for capture_windows** — `sources/capture_windows.py:24`:
   remove the second `DB` setting and use `config.DB_PATH`/`db.conn()` just like
   the app. Backward compatibility is not worth two database authorities; if
   `DB` is retained for one release, emit a deprecation warning when set and
   remove it from normal configuration docs. *Done when:* custom `DB_PATH`
   lands captures in the app's DB and no documented setting can silently send
   them elsewhere.
5. **Bound `_JOBS` and its tasks** — `app.py:641-681`: record `created_at` and
   `updated_at`, retain explicit task handles, cap total retained jobs, expire
   finished jobs by age, and mark working jobs timed out when they exceed the
   action deadline. Removing a dictionary entry does not stop the work, and
   cancelling `asyncio.to_thread()` does not stop its underlying thread: use a
   killable subprocess/process for cancellable jobs, or let an in-process job
   finish while it continues to hold the concurrency semaphore and retain its
   record until then. Preserve status long enough for the dashboard's
   five-minute polling window, and use a lock because completions and new
   requests mutate the registry concurrently.
6. **Harden list/status endpoints** — `app.py:114`: `limit=Query(200, ge=1,
   le=1000)`, `offset=Query(0, ge=0)`; `status()` `:106`: accumulate
   `by_kind` across types instead of overwriting. Validate comma-separated
   `types` against `config.PLAYABLE_TYPES`. Replace `str(e)` response bodies
   in generate/request/source/delete paths with stable error codes/messages and
   server-side exception logs; truncation alone can still disclose secrets.
7. **Guard `dashboard()`** — `app.py:718-723`: read with explicit UTF-8 and
   catch `OSError`, log it, then return a stable 500 response without a path or
   traceback. Test both missing and undecodable files.

---

## Phase 4 — Rendering, validation tuning, seeds

1. **ffmpeg filter-text safety** — `produce.py:206-235`: `_escape()` currently
   handles a font path, while `brand` is interpolated raw into `drawtext`'s
   `text=` value. Introduce context-specific escaping for both filter option
   values and drawtext text (backslash first, then `'`, `:`, `,`, `;`, `[`,
   `]`, and `%` as required by ffmpeg expansion), or preferably feed the brand
   through a temporary UTF-8 `textfile`. Do not apply a path-only helper to
   arbitrary text. Add a generated-filter test and a tiny ffmpeg integration
   test with `BRAND="a;b[c]:d'e%f"` and a font path containing punctuation.
2. **Clip/file identity collisions** — `produce.py:398-430`: the stem already
   includes window index `i`; the missing pieces are source identity beyond a
   truncated stem and per-render identity. Include a short hash of the source's
   normalized relative path plus the actual window tuple (or a render UUID) in
   both filename and row ID so same-prefix sources and later reruns never
   overwrite a file referenced by an older row. In `station_ids.py`, a
   per-invocation UUID plus `i` avoids same-second rerun collisions. Insert the
   row only after a successful render; check `rowcount`, and if registration
   fails remove the just-created unreferenced file. Append to `made`/print “ok”
   only after registration succeeds. Test two equal-prefix sources and two
   same-second invocations.
3. **Self-answer + truncation over-rejection** —
   `card_validation.py:115-118`: replace raw substring matching with a
   case-insensitive, escaped whole-token/phrase match while retaining the
   existing `len(otext) > 3` guard. This lets answer `Paris` pass a question
   containing `comparison` but still rejects a question that literally says
   `Paris`; a blanket “only ≥8 characters” rule would miss that real
   self-answer. At `:61`, remove the dangling-number rule: terminal numbers are
   valid facts and this function has no source metadata with which to infer
   truncation safely. Add all three cases as tests.
4. **`content_filter` word boundaries** — `content_filter.py:20-28`: split
   intentional stems (`bomb`, `genocide`, …) from chronic substring false
   positives. Use compiled word/form patterns, not just `\bexecut\b` (which
   would also stop matching `executed`/`execution`): cover forms of `shot`,
   `execute`, `coup`, `raid`, `shell`, `sink/sank/sunk`, `rape`, `riot`, and
   `crash` without matching `screenshot`, `executive`, `couple`, `afraid`,
   `eggshell`, `grape`, or `patriot`. Tests include those negatives plus
   `executed`, `bombing raid`, and `war` positives.
5. **`_m3u_attr` contract** — `app.py:359-369`: preserve commas; they are valid
   inside quoted attribute values and normal in the display title after the
   `#EXTINF` separator. Correct the docstring, which currently claims commas
   are replaced. Continue replacing quotes and CR/LF (the format has no
   portable quote escape and entries must remain one line). Test a title with
   quotes, commas, CR, and LF in the full emitted playlist.
6. **Seed quality** — `seed.py`: load registered local IDs first and probe only
   new files, eliminating the per-boot ffprobe storm. Make `_probe_duration`
   return `None` for unreadable/non-positive media and skip registration rather
   than inventing a healthy 30s row or adding an undocumented `suspect` health
   state. Map root-level files to explicit kind `unsorted`, not the
   `ASSET_ROOT` directory name. Add a missing-file sweep using the shared media
   resolver for every local file-backed type. Park missing video/image rows
   (`enabled=0`, `health='dead'`); for a rendered card, clear its stale `uri`
   so the renderer can rebuild it while preserving the operator's `enabled`
   choice. Never touch streams/remote URIs or delete history, and do not
   automatically re-enable operator-disabled rows merely because a file
   reappears.
7. **`_music_bed` path containment** — `render_cards.py:666-674`: reject
   absolute paths and resolve the candidate against `ASSET_ROOT`; require the
   resolved file to remain beneath the resolved root (including through
   symlinks), else return `None` for silence. A lexical `..` check alone is not
   containment. Add absolute, traversal, symlink-escape, and valid-path tests.
8. **`_bg_image` fetch/cache hardening** — `render_cards.py:305-327`: accept
   only HTTP(S) URLs, reject credentials, apply the same redirect/private-target
   policy chosen for outbound fetches, and cap actual downloaded bytes. Stream
   to a temporary file and atomically replace it only after Pillow verifies the
   image; remove corrupt/partial cache entries. Hash the full URL with SHA-256
   for the cache key instead of the collision-prone 120-character squash. Add
   `prune_bg_cache(max_age, max_bytes)` and call it from `tidy` (the cache
   otherwise grows forever). Test `file://`, redirect, oversize, corrupt image,
   cache-key collision, and eviction paths.
9. **`enrich_bg` scheduling + correctness** — `generators/enrich_bg.py`:
   compute the deadline inside `run()` (the import-time value expires in a
   long-lived process) and use `db.conn()`/`finally` for closure. Do not call
   `license_type=all` “CC/PD”: choose an explicit allowlist. The simplest
   redistributable default is CC0/public-domain-mark only; if attribution
   licenses are allowed, persist creator/title/license/license URL/source-page
   metadata and render or otherwise ship compliant attribution. Validate this
   against the current Openverse API schema and terms. Make the scheduling
   decision now: add it as overnight phase 1.5 before card rendering, bounded
   by its own timeout, and document the network/licensing behavior. Do not also
   add a weekly in-process job unless duplicate scheduling is explicitly
   desired.
10. **`_extract_count` ambiguous-number handling** — `ingest.py:504-510`:
    prefer explicit count grammar (`5 clips`, `pull 5`) instead of treating any
    standalone one- or two-digit number as a count. Preserve the default when a
    number is part of a year/decade or other theme (`cartoons from 40`), and
    preserve `pull 8 rain`/`5 golf clips` as explicit counts. Note that the
    review's original `1940s cartoons` repro already returns `None` with the
    current `\b\d{1,2}\b` regex; keep it as a regression case, but do not claim
    it currently returns 15.
11. **Weather `--n` honesty** — `generators/weather.py:100`: remove `--n` from
    the weather CLI and special-case API invocation; weather has exactly one
    card per location, so accepting and ignoring a count is misleading rather
    than useful symmetry. Update CLI/API docs and test that unsupported flags
    fail normally.
12. **Leap-day semantics** — `seasons._doy`: the current expression maps
    `02-29` to February 28 even in leap years. Construct February 29 normally
    first and fall back to February 28 only when that date is invalid in a
    common year. Test both leap and non-leap years, including a window/peak that
    contains February 29.

---

## Phase 5 — Dead code, docs, ops, CI

1. **Remove dead code:** remove `rotation.NEVER`, the unused `tag` parameter/
   `TAGS`/`del tag` path in `station_ids`, `app`'s `shlex` import/delete, and
   every import currently reported by `ruff check --select F401` (including
   `app`'s `os`/`time`/`FileResponse`, `cards`/`weather`/`produce`/`prune`/
   `render_cards`/`rotation`/`seed`, `live_cams.config`, `seasons.config`,
   and `fetch_queue.subprocess`).
   Fix `brandslam.font_pool(limit=0)` with
   `out[:limit] if limit is not None else out`. Add `ruff` F401/F821 to CI or
   keep a small explicit dead-code test so this does not immediately drift.
2. **`seasons.apply` disarm** — `seasons.py:175-254`: default the CLI to
   report-only and require `--apply` for the legacy in-place write (mirror
   `prune`). Make the default call `apply(..., dry_run=True)`; `--dry-run`
   beside a report-only default is otherwise meaningless, so replace it with
   `--apply`. Update CLI.md. Keep `restore_base_weights` and the hourly heal in
   `jobs.py` untouched.
3. **Retire `resolve_cams`** — remove `sources/resolve_cams.py` and its CLI
   documentation. It embeds a curated YouTube search list even though the
   shipped policy says no YouTube cams are enabled, its expiring URLs are not
   scheduled, and snapshot cams in `live_cams.yaml`/`capture_windows.py` are
   the maintained opt-in path. Add a changelog migration note for anyone who
   invoked it manually. This resolves the review's fork explicitly instead of
   leaving a “schedule or deprecate” decision for the implementer.
4. **Docs corrections:** INTEGRATION.md:20 `/random` returns up to `count`
   (default 5), not “one bumper”; CARDS.md trivia row says unlabelled options
   are auto-labelled and mixed/inconsistent labels are rejected;
   `grounded.py`'s usage path is `bumparr.generators`; `config.py` loses the
   deployment-specific “On Tower” wording. In `.env.example`, explain that
   compose-side `ASSETS`/`DATA` select host mounts while in-container
   `ASSET_ROOT`/`DB_PATH` select application paths; they are not aliases.
   State the trusted-LAN/no-auth threat model prominently in README and API.md
   beside destructive and subprocess-launching endpoints. Remove app.js's
   stale claim that the list endpoint omits payload. Also correct existing
   collateral inaccuracies: weather/local-time are rendered with TTLs (README
   and API.md currently say they are not), `tidy` removes debris rather than
   “dead entries,” and `healthz` is process liveness, not a DB check. Remove the
   unreachable model-`number` branches/default-verification claim from
   `generators/cards.py` and CLI.md; numbers intentionally come only from the
   grounded dataset.
5. **Dashboard hardening + real search** — `web/app.js`: do not pass server
   strings through the current `esc()` and then interpolate them into
   attributes; that helper is a text-context escape and does not make attribute
   construction safe. Build filter/card/video nodes with DOM APIs
   (`textContent`, `dataset`, and the `src` property) for `kind`, `title`,
   `type`, and `media_url`. Add a bounded `q` parameter to `/api/bumpers` and
   perform title/kind search server-side so search covers the full pool rather
   than only the current 24-row page. Test hostile quote/markup values and a
   match beyond page one.
6. **Ops** — `Dockerfile`: run as a fixed non-root UID/GID and create/chown the
   image-owned working directories. Use Docker-managed volumes by default so a
   clean checkout is writable on first run; document that optional bind-mounted
   asset/data directories must be writable by that UID. Add a `HEALTHCHECK`
   using Python's stdlib because the slim image
   does not install curl. Do not hard-pin one Debian ffmpeg package version
   that disappears when the base repository advances; pin the Python base by
   digest for reproducibility and document that apt security packages
   intentionally float within that base snapshot (or use a fully snapshotted
   Debian repository). In compose, warn that the `./bumparr:/app/bumparr:ro`
   development mount shadows the code baked into the image and should be
   removed for release-pinned deployments. CI adds `python -m compileall -q
   bumparr`, `node --check bumparr/web/app.js`, the targeted ruff check, and a
   clean-checkout `docker compose up` build/start/write smoke step; add a pinned
   `ruff` dev/CI dependency and pin
   third-party Actions to commit SHAs.
7. **Killed suspicions (do NOT "fix"):** `SHARE_*` dead settings — no such
   vars exist, killed by grep. `brandslam.face_at` O(n²) — negligible at these
   sizes (≤ ~30 schedule marks). `/fill` route ordering — correct, the in-code
   warning holds. `on_this_day` LIKE fragility — verified working for both
   payload shapes; leave a comment, not a migration (a future payload writer
   must keep `": "` separators or switch matching to parsed JSON).

---

## Phase 6 — Tests (land alongside the fixes above)

New files (unittest style, following `tests/test_card_validation.py`):

| File | Covers (review's "highest-value" list) |
|---|---|
| `tests/test_stream_proxy.py` | H1 signed-token/pid binding, origin/CDN allowlist, redirect policy, index/segment size caps |
| `tests/test_paths.py` | M1 media/category containment, absolute and symlink escapes; M10 compensating deletion |
| `tests/test_fetch_queue.py` | H2 filename/category traversal, atomic replacement, declared-vs-actual cap |
| `tests/test_grounded.py` | M2 saturation cap/outcome counts; M8 deterministic baseline plus explicit expansion |
| `tests/test_cards_gen.py` | M3 per-item survival and DB-error propagation (stub model) |
| `tests/test_weather.py` | M4 stat/uri preservation |
| `tests/test_live_cams.py` | M5 document/item validation, stable IDs, health/enabled preservation, removal parks |
| `tests/test_jobs.py` | M6 isolated capture/fetch failures, stat errors, cancellation; job expiry/concurrency |
| `tests/test_downloads.py` | M7 actual-byte caps, atomic download/capture replacement, partial cleanup |
| `tests/test_ingest_routing.py` | M9 weather-vs-footage; explicit vs ambiguous count grammar; category sanitization |
| `tests/test_rotation.py` | never-played-first, recency floor, fatigue clamp, `explain` consistency |
| `tests/test_seasons.py` | window/lead/peak/tail curves, wraparound, leap/common-year Feb-29, `restore_base_weights` |
| `tests/test_validation_extra.py` | self-answer/substring, dangling-number, grim word-boundary matrix |
| `tests/test_processes.py` | ffmpeg timeout kill/reap/cleanup and successful stderr drain |
| `tests/test_seed.py` | new-only probing, unreadable/root-level/missing-file behavior |
| `tests/test_render_safety.py` | music/background containment, URL/size/cache policy, filter-text escaping |
| `tests/test_produce_identity.py` | same-prefix source and same-second invocation collisions; registration failure cleanup |
| `tests/test_db.py` | connection closes on success/error and transaction rollback semantics |
| `tests/test_app_api.py` | `/fill` exactness/empty pool, `/random` defaults, DELETE traversal/404s, bounds/types/search, jobs, M3U |
| `bumparr/web/app.test.js` | Node built-in test + a minimal DOM fixture: hostile API strings create no markup/attribute injection; search beyond first page |

Also fix `tests/test_card_seeds.py:16`'s unclosed file (ResourceWarning), and
the same `open()`-without-close idiom in `ingest._load_card_seeds`,
`grounded.gen_number`, and `capture_windows._load_cams` (use `with open(...,
encoding="utf-8")`). Run the suite with warnings promoted to errors at least
once so new resource leaks are visible.

**Final gate (matches the review baseline):**

```bash
PYTHON=${PYTHON:-python3}
"$PYTHON" -m compileall -q bumparr
"$PYTHON" -W error::ResourceWarning -m unittest discover -s tests -v
node --check bumparr/web/app.js
node --test bumparr/web/app.test.js
ruff check --select F401,F821 bumparr tests
docker build .
```

---

## Suggested landing order (checklist)

- [x] Phase 1: H1, H2, M1 (+ regression tests) — one PR, security label; do not publish exploit details before a patched release
- [x] Phase 2: M2–M10 (+ tests) — one PR per 2–3 items, each independently shippable
- [x] Phase 3: process/timeout/connection hygiene
- [x] Phase 4: rendering + validation tuning (visible pool-behavior changes — note in CHANGELOG)
- [x] Phase 5: dead code, docs, ops, CI
- [x] Phase 6: backfill remaining tests; keep `compileall` + full suite green throughout

The fixes preserve the scoring model and fail-closed validation. Release notes
must call out the intentional contract changes: proxy tokens become signed and
allowlisted; configured cams gain stable IDs; long-running actions return job
IDs; the obsolete `resolve_cams` module and weather `--n` flag disappear. The
number baseline and destructive CLI defaults become genuinely idempotent/dry-run
as originally intended.
