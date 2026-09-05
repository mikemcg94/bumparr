"""Bumparr — standalone bumper generator for the *arr stack.

Copyright (c) 2026 the Bumparr authors. Licensed under the GNU AGPL-3.0 (see LICENSE).
This program is free software; modified/network-deployed versions must offer
their complete source under the same license.

Runs as its own service (own port, REST API, web dashboard), independent of any
channel brain. Point it at your sources (media, public-domain archives, live cams,
a local model, your fonts) and it builds and maintains a self-refreshing pool of
bumpers / interstitials that any channel generator (Dispatcharr, ErsatzTV, Tunarr)
or player can consume.

Run:  uvicorn bumparr.app:app --host 0.0.0.0 --port 8780
"""
import asyncio
import json
import logging
import random
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from bumparr import config, db, seed, live_cams, stream_proxy, ingest, paths
from bumparr.urls import absolutize as _absolutize
from bumparr.station import routes as station_routes

WEB_DIR = Path(__file__).resolve().parent / "web"
log = logging.getLogger(__name__)


async def _volatile_refresh_loop():
    """Keep perishable cards truthful.

    A clock or weather card rendered to a file goes stale by definition, so the
    file is re-rendered on a cadence matched to how fast its content changes —
    the same treatment the live-window cams already get. Runs off the playback
    path and no-ops when nothing has aged out.
    """
    import asyncio
    from bumparr import render_cards
    interval = int(config.env("VOLATILE_INTERVAL", "60"))
    while True:
        try:
            await asyncio.sleep(interval)
            res = await asyncio.to_thread(render_cards.refresh_volatile)
            n = res["stats"].get("rendered", 0)
            if n:
                print("[bumparr] refreshed %d perishable card(s)" % n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[bumparr] volatile refresh error: %s" % e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown: bring the pool up, then run the background jobs.

    Everything here is additive and idempotent, so a restart never loses or
    duplicates content: seeding registers what is on disk, baselines register
    the model-free card floor, cams upsert from config. The three loops are the
    self-maintenance: perishable cards re-render, live windows re-capture, and
    dated cards / seasonal weights re-evaluate.
    """
    import asyncio
    db.init_db()
    added = seed.seed_from_assets()
    base = ingest.register_all_baselines()
    try:
        cams = live_cams.load_cams()
    except Exception as e:
        print("[bumparr] live_cams load failed: %s" % e)
        cams = 0
    print(f"[bumparr] startup: {added} new bumper(s) seeded, {base} baseline card(s), {cams} live cam(s) loaded")
    from bumparr import jobs
    tasks = [asyncio.create_task(_volatile_refresh_loop()),
             asyncio.create_task(jobs.window_refresh_loop()),
             asyncio.create_task(jobs.dated_card_loop()),
             asyncio.create_task(jobs.station_conform_loop())]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        with _JOB_LOCK:
            action_tasks = list(_JOB_TASKS.values())
        for task in action_tasks:
            task.cancel()
        await asyncio.gather(*tasks, *action_tasks, return_exceptions=True)


app = FastAPI(title="Bumparr", lifespan=lifespan)
app.include_router(stream_proxy.router)
app.include_router(station_routes.router)


class _StationSegmentFiles(StaticFiles):
    """Serve MPEG-TS segments independently of the host MIME database."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if Path(full_path).suffix.lower() == ".ts":
            response.headers["content-type"] = "video/mp2t"
        return response


# ---------- Status / pool inspection ----------

@app.get("/api/status")
def status():
    """Pool overview: total vs currently-airable counts, split by type and kind.

    `playable_now` is the enabled-and-healthy count before dynamic seasonal or
    duration filtering. The pool can retain disabled/dead rows for history.
    """
    with db.conn() as c:
        rows = c.execute("SELECT type, kind, source, COUNT(*) n, SUM(CASE WHEN enabled=1 AND health='ok' THEN 1 ELSE 0 END) live "
                         "FROM playables GROUP BY type, kind").fetchall()
    by_type, by_kind = {}, {}
    total = live = 0
    for r in rows:
        by_type[r["type"]] = by_type.get(r["type"], 0) + r["n"]
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + r["n"]
        total += r["n"]
        live += r["live"]
    return {"brand": config.BRAND, "total": total, "playable_now": live,
            "by_type": by_type, "by_kind": by_kind}


@app.get("/api/bumpers")
def list_bumpers(request: Request, type: str = None, kind: str = None,
                 enabled: bool = None,
                 q: str = Query(None, max_length=100),
                 limit: int = Query(200, ge=1, le=1000),
                 offset: int = Query(0, ge=0)):
    """Browse the whole pool, newest first.

    Filter by `type` (video | card | stream | image), `kind` and/or `enabled`;
    paginate with `limit`/`offset`. Includes disabled and unhealthy rows — this
    is the management view, not a source of playable material (that is /random,
    /fill and /playlist.m3u). `payload` is the parsed JSON card content, if any.

    `enabled` defaults to None rather than False on purpose: absent has to mean
    "no filter", or the default listing would silently hide every parked row.
    `enabled=false` is how an operator finds what the system parked without
    paging the whole pool by eye; POST /api/pool/enable is the way back.
    """
    if type and type not in config.PLAYABLE_TYPES:
        return JSONResponse({"error": "invalid playable type"}, status_code=400)
    sql = "SELECT * FROM playables WHERE 1=1"
    args = []
    if type:
        sql += " AND type=?"; args.append(type)
    if kind:
        sql += " AND kind=?"; args.append(kind)
    if enabled is not None:
        # 0 is the parked marker every writer uses; anything else counts as on,
        # the same reading live_cams.load_cams applies.
        sql += " AND enabled!=0" if enabled else " AND enabled=0"
    if isinstance(q, str) and q.strip():
        term = "%" + q.strip().replace("%", "\\%").replace("_", "\\_") + "%"
        sql += " AND (title LIKE ? ESCAPE '\\' OR kind LIKE ? ESCAPE '\\')"
        args += [term, term]
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"; args += [limit, offset]
    with db.conn() as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except Exception:
            payload = {}
        out.append({"id": r["id"], "type": r["type"], "kind": r["kind"],
                    "source": r["source"], "duration": r["duration"], "title": r["title"],
                    "tags": r["tags"], "enabled": r["enabled"], "health": r["health"],
                    "media_url": _media_url(r, request), "payload": payload})
    return {"count": len(out), "bumpers": out}


def _media_url(row, request=None):
    """The URL a consumer should play for this row, or None if unplayable.

    video/image (and rendered card) rows resolve to /media/<uri>; stream rows
    either pass their upstream URL through (direct, CORS-able feeds) or route
    through the same-origin HLS proxy.
    """
    # 'card' is here because a rendered card IS a media file. Until it is
    # rendered its uri is NULL and it stays correctly invisible to consumers.
    if row["type"] in ("video", "image", "card") and row["uri"]:
        return _absolutize("/media/" + row["uri"], request)
    if row["type"] == "stream":
        try:
            p = json.loads(row["payload"] or "{}")
        except Exception:
            p = {}
        raw = row["uri"] if p.get("direct") else "/api/stream/%s/index.m3u8" % row["id"]
        return _absolutize(raw, request)
    return None


@app.get("/api/bumpers/random")
def random_bumpers(request: Request,
                   count: int = Query(5, ge=1, le=100),
                   max_duration: float = Query(None, gt=0, le=86400),
                   types: str = Query(None, description="comma list, e.g. video,card")):
    """The output contract for channel generators: hand me N bumpers (optionally
    capped by duration / restricted to types) and I return playable items."""
    type_filter = {t.strip() for t in types.split(",")} if types else None
    if type_filter and not type_filter.issubset(config.PLAYABLE_TYPES):
        return JSONResponse({"error": "invalid playable type"}, status_code=400)
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM playables WHERE enabled=1 AND health='ok'").fetchall()]
    pool = []
    for r in rows:
        if type_filter and r["type"] not in type_filter:
            continue
        if not _media_url(r, request):
            continue
        # Zero weight means seasonally gated off the air, not merely unlikely.
        if (r["weight"] or 0) <= 0:
            continue
        if max_duration and (r["duration"] or 0) > max_duration:
            continue
        pool.append(r)
    if not pool:
        return {"count": 0, "bumpers": []}
    # Same rotation model the player uses, so a channel generator pulling from
    # here gets the same variety rather than a naive weighted shuffle.
    from bumparr import dayparts, rotation, seasons
    try:
        season = seasons.factors_now()
    except Exception:
        season = {}
    try:
        daypart = dayparts.factors_now()
    except Exception:
        daypart = {}
    weights, _ = rotation.weights_for(pool, season, None, daypart)
    weights = [max(0.0001, w) for w in weights]
    picks = random.choices(pool, weights=weights, k=min(count, len(pool) * 3))
    seen, out = set(), []
    for r in picks:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        try:
            payload = json.loads(r["payload"] or "{}")
        except Exception:
            payload = {}
        out.append({"id": r["id"], "type": r["type"], "kind": r["kind"],
                    "title": r["title"], "duration": r["duration"],
                    "media_url": _media_url(r, request), "payload": payload})
        if len(out) >= count:
            break
    return {"count": len(out), "bumpers": out}


# NOTE: this MUST stay above /api/bumpers/{bumper_id:path}. That route
# matches any trailing segment, so declared first it would swallow "fill"
# and return 404 for a lookup of a bumper id that does not exist.
@app.get("/api/bumpers/fill")
def fill(request: Request,
         seconds: float = Query(..., gt=0, le=86400, description="the gap to fill"),
         tolerance: float = Query(1.5, ge=0, le=3600, description="acceptable over/under, seconds"),
         max_items: int = Query(8, ge=1, le=40),
         types: str = Query(None, description="comma list, e.g. video,card")):
    """Hand back bumpers that add up to a requested gap.

    This is the contract a channel generator actually needs: "the next show
    starts in 47 seconds, give me something to run." Existing filler systems
    only play whole items that happen to fit and eat the remainder as dead air.

    Solved as a small subset-sum with randomised restarts rather than a greedy
    pass, because greedy leaves a stubborn remainder that no single clip covers
    — which is exactly the gap the caller wanted closed. Short bumpers are the
    change that makes an exact total reachable, so a pool without them will
    report a wider gap here rather than silently return a bad fit.
    """
    type_filter = set(t.strip() for t in types.split(",")) if types else None
    if type_filter and not type_filter.issubset(config.PLAYABLE_TYPES):
        return JSONResponse({"error": "invalid playable type"}, status_code=400)
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM playables WHERE enabled=1 AND health='ok' "
            "AND uri IS NOT NULL AND uri!=''").fetchall()]

    pool = []
    for r in rows:
        if type_filter and r["type"] not in type_filter:
            continue
        if (r["weight"] or 0) <= 0:
            continue          # seasonally gated off the air
        d = float(r["duration"] or 0)
        if 0 < d <= seconds + tolerance:
            pool.append((d, r))
    if not pool:
        return {"requested": seconds, "total": 0.0, "gap": seconds,
                "exact": False, "count": 0, "bumpers": [],
                "note": "no bumper is short enough for this gap"}

    best, best_err = [], None
    for attempt in range(240):
        rng = random.Random(attempt)
        picks, total, used = [], 0.0, set()
        candidates = pool[:]
        rng.shuffle(candidates)
        while len(picks) < max_items:
            remaining = seconds - total
            # Prefer the largest clip that still fits; fall back to the closest.
            fits = [(d, r) for d, r in candidates
                    if r["id"] not in used and d <= remaining + tolerance]
            if not fits:
                break
            fits.sort(key=lambda dr: abs(dr[0] - remaining))
            window = fits[:4] if len(fits) > 4 else fits
            d, r = window[rng.randrange(len(window))]
            picks.append(r)
            used.add(r["id"])
            total += d
            if abs(seconds - total) <= 0.05:
                break
        err = abs(seconds - total)
        if best_err is None or err < best_err:
            best, best_err = picks, err
            if err <= 0.05:
                break

    total = sum(float(r["duration"] or 0) for r in best)
    out = []
    for r in best:
        try:
            payload = json.loads(r["payload"] or "{}")
        except Exception:
            payload = {}
        out.append({"id": r["id"], "type": r["type"], "kind": r["kind"],
                    "title": r["title"], "duration": r["duration"],
                    "media_url": _media_url(r, request), "payload": payload})
    return {"requested": seconds, "total": round(total, 2),
            "gap": round(seconds - total, 2),
            "exact": abs(seconds - total) <= tolerance,
            "count": len(out), "bumpers": out}


@app.get("/api/bumpers/{bumper_id:path}")
def get_bumper(bumper_id: str, request: Request = None):
    """One bumper as JSON: every registry column plus a resolved media_url."""
    with db.conn() as c:
        r = c.execute("SELECT * FROM playables WHERE id=?", (bumper_id,)).fetchone()
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    d = dict(r)
    d["media_url"] = _media_url(r, request)
    return d


# ---------- M3U output (for IPTV-style consumers) ----------

def _m3u_attr(value):
    """Make a value safe inside a double-quoted M3U attribute.

    M3U attributes are double-quoted and the format defines no escape sequence,
    so a title containing a quote (trivia questions quote song and book titles
    constantly) silently truncates the attribute and corrupts the entry for the
    parser. Newlines get the same treatment because #EXTINF is a single line
    whose display name runs to end-of-line. Commas are deliberately preserved:
    inside quoted attributes they are legal and naive parsers split only the
    unquoted display-name separator.
    """
    s = str(value or "")
    return s.replace('"', "'").replace("\n", " ").replace("\r", " ").strip()



@app.get("/playlist.m3u")
def playlist_m3u(request: Request):
    """An M3U of the video/stream bumpers, for IPTV tooling that ingests playlists.

    Entries are ABSOLUTE URLs. IPTV consumers fetch this playlist and then hand
    each entry to a separate player/transcoder process, which has no memory of
    where the playlist came from, so a relative path would be unresolvable.
    """
    lines = ["#EXTM3U"]
    with db.conn() as c:
        rows = c.execute("SELECT * FROM playables WHERE enabled=1 AND health='ok' "
                         "AND (type='stream' OR (type IN ('video','card') "
                         "AND uri IS NOT NULL AND uri!='')) "
                         "ORDER BY kind").fetchall()
    for r in rows:
        url = _media_url(r, request)
        if not url:
            continue
        title = _m3u_attr(r["title"])
        lines.append('#EXTINF:-1 tvg-name="%s" group-title="%s",%s'
                     % (title, _m3u_attr(r["kind"]), title))
        lines.append(url)
    return PlainTextResponse("\n".join(lines) + "\n", media_type="audio/x-mpegurl")


# ---------- Actions: generate + refresh sources ----------

def _run(mod, *args):
    """Run a bumparr CLI module as a subprocess and capture its output.

    Subprocess rather than in-process call because the modules do heavy
    blocking work (ffmpeg, downloads) that must not hold the event loop, and
    because their in-memory state (DB connection pragmas, caches) is set up in
    their `__main__` paths.
    """
    return subprocess.run([sys.executable, "-m", mod, *args],
                          capture_output=True, text=True, timeout=60 * 30)


def _resolve_media(uri):
    """Registry uri -> file on disk, across the source and output trees."""
    return paths.resolve_media(uri)


@app.delete("/api/bumpers/{bumper_id:path}")
def delete_bumper(bumper_id: str, keep_file: bool = False):
    """Remove one bumper: its registry row and, by default, its file.

    Deleting the row alone is not enough for anything that came from disk — the
    asset scan would find the orphaned file on the next restart and register it
    right back. So the file goes too unless explicitly kept. Live streams have
    no local file and only lose their row.
    """
    staged = None
    path = None
    try:
        with db.conn() as c:
            row = c.execute("SELECT id, type, uri, kind, title FROM playables WHERE id=?",
                            (bumper_id,)).fetchone()
            if row is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            row = dict(row)
            uri = row["uri"] or ""
            path = _resolve_media(uri)
            if path is None and uri and not uri.startswith(("http://", "https://")):
                log.warning("row-only deletion for unsafe media URI on %s", bumper_id)
            if path is not None and not keep_file:
                staged = paths.stage_delete(path)
            c.execute("DELETE FROM playables WHERE id=?", (bumper_id,))
    except Exception as exc:
        log.error("delete failed for %s: %s", bumper_id, exc)
        try:
            if path is not None:
                paths.restore_delete(path, staged)
        except OSError as restore_exc:
            log.critical("could not restore staged file for %s: %s", bumper_id, restore_exc)
        return JSONResponse({"error": "delete failed"}, status_code=500)

    cleanup_failed = False
    try:
        paths.finish_delete(staged)
    except OSError as exc:
        cleanup_failed = True
        log.error("quarantine cleanup failed for %s: %s", bumper_id, exc)
    file_removed = staged is not None and not cleanup_failed
    # Deleting the last item in a category leaves an empty directory behind. It
    # is harmless to playback but it keeps a dead category visible to anything
    # that scans the tree, so tidy it up here rather than leaving a stray.
    dir_removed = False
    if file_removed and path is not None:
        try:
            if path.parent.is_dir() and not any(path.parent.iterdir()) \
                    and path.parent not in (Path(config.ASSET_ROOT), Path(config.OUTPUT_DIR)):
                path.parent.rmdir()
                dir_removed = True
        except Exception:
            pass
    return {"deleted": bumper_id, "kind": row["kind"], "title": row["title"],
            "file_removed": file_removed, "dir_removed": dir_removed,
            "cleanup_failed": cleanup_failed}


@app.delete("/api/pool/kind/{kind}")
def delete_kind(kind: str, keep_files: bool = False):
    """Remove a whole category — the usual fix when a search returned junk.

    Also removes the now-empty source directory, since leaving it means the next
    asset scan re-registers anything still sitting inside it.
    """
    kind_dirs = [paths.resolve_kind_dir(root, kind)
                 for root in (Path(config.ASSET_ROOT), Path(config.OUTPUT_DIR))]
    if any(d is None for d in kind_dirs):
        return JSONResponse({"error": "invalid category"}, status_code=400)
    removed, failed, staged_files = 0, [], []
    try:
        with db.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, uri FROM playables WHERE kind=?", (kind,)).fetchall()]
            for r in rows:
                path = _resolve_media(r["uri"] or "")
                staged = None
                if path is not None and not keep_files:
                    try:
                        staged = paths.stage_delete(path)
                    except OSError:
                        failed.append({"id": r["id"], "error": "file staging failed"})
                        continue
                try:
                    c.execute("DELETE FROM playables WHERE id=?", (r["id"],))
                except Exception:
                    if path is not None:
                        paths.restore_delete(path, staged)
                    raise
                if staged is not None:
                    staged_files.append((path, staged))
                removed += 1
    except Exception as exc:
        log.error("bulk delete failed for %s: %s", kind, exc)
        for original, staged in reversed(staged_files):
            try:
                paths.restore_delete(original, staged)
            except OSError as restore_exc:
                log.critical("bulk restore failed for %s: %s", kind, restore_exc)
        return JSONResponse({"error": "delete failed"}, status_code=500)

    for original, staged in staged_files:
        try:
            paths.finish_delete(staged)
        except OSError:
            failed.append({"id": original.name, "error": "quarantine cleanup failed"})
    dirs = 0
    if not keep_files:
        for d in kind_dirs:
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                    dirs += 1
                except Exception:
                    pass
    return {"kind": kind, "removed": removed, "dirs_removed": dirs,
            "failed": failed[:10]}


@app.post("/api/pool/tidy")
def tidy(dry_run: bool = False):
    """Clear debris that accumulates around a working pool.

    Two kinds. Zero-byte files, left when a download fails partway, which are
    unplayable and which a later scan may still try to register. And empty
    category directories, left when the last item in a category is deleted,
    which keep a dead category visible to anything walking the tree.
    """
    if not dry_run:
        try:
            from bumparr import render_cards
            getattr(render_cards, "prune_bg_cache", lambda: 0)()
        except Exception as exc:
            log.warning("background cache pruning failed: %s", exc)
    empties, zero = [], []
    for root in {Path(config.ASSET_ROOT), Path(config.OUTPUT_DIR)}:
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if f.is_file() and f.stat().st_size == 0:
                zero.append(str(f.relative_to(root)))
                if not dry_run:
                    try:
                        f.unlink()
                    except Exception:
                        pass
        # Deepest-first, so a directory emptied by removing its subdirectory is
        # itself considered in the same pass.
        for d in sorted((p for p in root.rglob("*") if p.is_dir()),
                        key=lambda p: len(p.parts), reverse=True):
            try:
                if not any(d.iterdir()):
                    empties.append(str(d.relative_to(root)))
                    if not dry_run:
                        d.rmdir()
            except Exception:
                pass
    return {"zero_byte_files": len(zero), "empty_dirs": len(empties),
            "removed_files": zero[:20], "removed_dirs": empties[:20],
            "dry_run": dry_run}


@app.post("/api/pool/revive")
def revive(dry_run: bool = False):
    """Restore items the system retired whose media is actually fine.

    One thing retires an item without a human deciding to: the asset sweep,
    which parks a row whose file it can no longer find (seed.py sets enabled=0,
    health='dead'). That verdict can be wrong — a mount that came up late, a
    snippet caught mid-rewrite — and it is not an operator saying "switch this
    off", so this re-checks each parked row against reality and clears the park
    only when ffprobe can still read the file. health='dead' is the retirement
    marker; re-examining it is this endpoint's whole job.

    The schema also reserves health and fail_count for a playback-failure
    reporter (docs/SCHEMA.md). Nothing ships one today — the sweep is the only
    writer of health='dead' — but the same re-check is what would clear its
    verdicts, which is why the UPDATE resets fail_count as well.

    on_this_day cards are excluded by kind, and that exclusion is a workaround
    rather than a design. `enabled=0` carries three unrelated meanings — an
    operator switching a row off, a system park, and the on_this_day calendar
    rotation — and nothing on the row records which one wrote it, so a sweep
    over every parked row cannot ask. The kind name is the only proxy there is,
    and it happens to be exact: the rotation is the sole writer of that kind's
    `enabled`. Without the exclusion this would put every wrong-date card back
    on air. Live streams are skipped because their health depends on the far
    end — POST /api/pool/enable is the deliberate way to bring one back.
    """
    restored, still_dead, skipped = [], [], []
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, type, uri FROM playables "
            "WHERE (health='dead' OR enabled=0) "
            "AND (kind IS NULL OR kind != 'on_this_day')").fetchall()]
    for r in rows:
        uri = r["uri"] or ""
        if r["type"] == "stream" or uri.startswith(("http://", "https://")):
            skipped.append(r["id"])
            continue
        path = paths.resolve_media(uri)
        if path is None or not path.is_file() or path.stat().st_size == 0:
            still_dead.append(r["id"])
            continue
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            still_dead.append(r["id"])
            continue
        if probe.returncode == 0 and probe.stdout.strip():
            restored.append(r["id"])
        else:
            still_dead.append(r["id"])
    if restored and not dry_run:
        with db.conn() as c:
            c.executemany(
                "UPDATE playables SET health='ok', fail_count=0, enabled=1 WHERE id=?",
                [(i,) for i in restored])
            c.commit()
    return {"checked": len(rows), "restored": len(restored),
            "still_dead": len(still_dead), "skipped_streams": len(skipped),
            "dry_run": dry_run}


def _calendar_park_warning(kind, payload):
    """The sentence to attach when `enabled` on this row is not the operator's.

    `enabled` is one column carrying three unrelated claims. Two of them are a
    person or a verifiable fact — operator intent, and a system park whose cause
    can be re-checked (a missing file, a de-configured cam). The third is a
    schedule: on_this_day cards are parked and un-parked purely by the calendar
    (generators/on_this_day.retire_other_days), and nothing on the row records
    that the 0 came from there. So enabling one looks exactly like enabling
    anything else and then quietly comes undone — jobs.dated_card_loop runs the
    rotation on startup and every hour after, parking every card whose payload
    `for_date` is not today.

    Refusing would be worse than the surprise: the operator named an id, and
    naming an id is the decision. So the endpoint does what it was asked and
    says what will happen to it. Returns None for rows no schedule owns.

    The kind test is exact rather than heuristic: on_this_day is the only kind
    the rotation writes, and the rotation is the only writer of that kind's
    `enabled`. The date test is the rotation's own predicate
    (on_this_day.is_todays_card), so the sentence and the next pass give the
    same answer. They did not always: this parsed the payload while the
    rotation matched the serialized text with SQL LIKE, which disagreed on a
    compact-serialized card and on a NULL payload. `for_date` is read here only
    to name the day in the sentence, never to decide.
    """
    if kind != "on_this_day":
        return None
    from bumparr.generators import on_this_day
    try:
        for_date = (json.loads(payload or "{}") or {}).get("for_date")
    except Exception:
        for_date = None
    today = on_this_day.today_key()
    when = ("the dated-card rotation (bumparr.jobs.dated_card_loop — on "
            "startup, then hourly)")
    if on_this_day.is_todays_card(payload, today):
        return ("This card is rotated by date and belongs to today (%s), so it "
                "stays on until the date rolls over; %s parks it again then."
                % (today, when))
    return ("This card is parked by date, not by an operator: it belongs to %s "
            "and today is %s. %s will park it again on its next pass, within "
            "the hour." % (for_date or "another day", today, when.capitalize()))


@app.post("/api/pool/enable")
def enable_playable(bumper_id: str):
    """Turn one parked row back on, no questions asked.

    `enabled` is operator intent: loaders and sweeps may park a row but never
    un-park one, so something has to speak for the operator. Revive covers what
    it can physically verify; this covers what it cannot — a live cam re-added
    to the YAML, or anything the operator simply wants back. Health is left
    alone: whether the far end answers is not this endpoint's claim to make.

    For a cam dropped from live_cams.yaml the order matters: put it back in the
    YAML and reload first, then call this. load_cams parks every live-cam row
    outside the configured set on every run, so enabling one the file still does
    not list holds only until the next restart — and load_cams never re-enables
    a cam it does find, which is why this second step remains necessary once the
    entry is back. This is the other half of re-adding a cam, not a shortcut
    past it.

    Unlike revive, this does not exempt on_this_day cards. Revive sweeps
    everything it can find and must not undo the calendar's rotation; here the
    operator named one id, and naming an id is the decision. What it does owe
    them is the truth about what they just asked for: a card the calendar owns
    gets an extra `warning` key saying the rotation will take it back, and when
    (see _calendar_park_warning). The `{id, enabled, changed}` keys are
    unchanged and always present; `warning` is added only when there is one.
    """
    with db.conn() as c:
        row = c.execute("SELECT id, enabled, kind, payload FROM playables WHERE id=?",
                        (bumper_id,)).fetchone()
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        changed = not row["enabled"]
        if changed:
            c.execute("UPDATE playables SET enabled=1 WHERE id=?", (bumper_id,))
    out = {"id": bumper_id, "enabled": True, "changed": changed}
    warning = _calendar_park_warning(row["kind"], row["payload"])
    if warning:
        out["warning"] = warning
    return out


@app.post("/api/starter")
async def starter(dry_run: bool = False, only_free: bool = False,
                  limit: int = Query(None, ge=1, le=1000)):
    """Populate a fresh pool from the shipped starter seeds.

    Opt-in, never automatic: pulling gigabytes because a container started is
    not a decision to make on a user's behalf, and the archives throttle bursts.
    """
    args = []
    if dry_run:
        args.append("--dry-run")
    if only_free:
        args.append("--only-free")
    if limit:
        args += ["--limit", str(limit)]
    def work():
        r = _run("bumparr.starter", *args)
        return {"ok": r.returncode == 0,
                "stdout": (r.stdout or "")[-6000:],
                "stderr": (r.stderr or "")[-1500:]}
    return _start_job("starter", work)


@app.post("/api/render/cards")
async def render_cards(limit: int = Query(None, ge=1, le=1000), force: bool = False):
    """Render text cards to MP4 so non-browser consumers can play them.

    Offline and idempotent — an already-rendered card is skipped unless forced.
    Use `limit` to render in batches; a full pass over a large pool can outlast
    a comfortable request timeout.
    """
    args = []
    if limit:
        args += ["--limit", str(limit)]
    if force:
        args += ["--force"]
    def work():
        r = _run("bumparr.render_cards", *args)
        return {"ok": r.returncode == 0,
                "stdout": (r.stdout or "")[-4000:],
                "stderr": (r.stderr or "")[-2000:]}
    return _start_job("render cards", work)


@app.post("/api/station/conform")
async def station_conform(limit: int = Query(25, ge=1, le=1000)):
    """Prepare the pool for the live channels: conform up to `limit` items now.

    The background loop does this on its own; the action exists so a fresh
    install can see the channel fill without waiting for the interval.
    """
    from bumparr.station import conform, playout

    def work():
        return conform.sweep(limit=limit, keep=playout.active_keys())
    return _start_job("station conform", work)


@app.post("/api/generate/{kind}")
async def generate(kind: str, n: int = Query(20, ge=1, le=100)):
    """Generate cards. Grounded kinds (trivia, fun_facts, number) use real
    sources; the absurd kinds (psa, etc.) use the local model."""
    grounded = {"trivia", "fun_facts", "number"}
    model_kinds = {"psa", "corrections", "achievements",
                   "coming_up", "tiny_games"}
    def work():
        if kind in grounded:
            r = _run("bumparr.generators.grounded", "--kind", kind, "--n", str(n))
        elif kind == "on_this_day":
            r = _run("bumparr.generators.on_this_day", "--n", str(n))
        elif kind == "weather":
            r = _run("bumparr.generators.weather")
        elif kind in model_kinds:
            r = _run("bumparr.generators.cards", "--kind", kind, "--n", str(n))
        return {"kind": kind, "ok": r.returncode == 0,
                "output": (r.stdout or r.stderr)[-500:]}
    if kind not in grounded | model_kinds | {"on_this_day", "weather"}:
        return JSONResponse({"error": "unknown kind"}, status_code=400)
    return _start_job("generate %s" % kind, work)


_JOBS = {}
_JOB_TASKS = {}
_JOB_LOCK = threading.RLock()
_JOB_SEMAPHORE = asyncio.Semaphore(2)
_JOB_LIMIT = 100
_JOB_TTL = 60 * 60
_JOB_DEADLINE = 30 * 60


def _prune_jobs(now=None):
    now = now or time.time()
    with _JOB_LOCK:
        expired = [jid for jid, job in _JOBS.items()
                   if job["status"] != "working" and not job.get("worker_active", False)
                   and now - job.get("updated_at", now) > _JOB_TTL]
        for jid in expired:
            _JOBS.pop(jid, None)
        finished = sorted(
            ((job.get("updated_at", 0), jid) for jid, job in _JOBS.items()
             if job["status"] != "working" and not job.get("worker_active", False))
        )
        while len(_JOBS) >= _JOB_LIMIT and finished:
            _, jid = finished.pop(0)
            _JOBS.pop(jid, None)


def _start_job(label, func, deadline=_JOB_DEADLINE):
    """Run blocking work in the bounded action registry and return its job id."""
    _prune_jobs()
    now = time.time()
    with _JOB_LOCK:
        if len(_JOBS) >= _JOB_LIMIT:
            return JSONResponse({"error": "job capacity reached"}, status_code=429)
        job_id = uuid.uuid4().hex[:12]
        _JOBS[job_id] = {"status": "working", "result": None,
                         "request": label, "created_at": now, "updated_at": now,
                         "worker_active": True}

    async def runner():
        try:
            await asyncio.wait_for(_JOB_SEMAPHORE.acquire(), timeout=deadline)
        except asyncio.TimeoutError:
            with _JOB_LOCK:
                _JOBS[job_id].update(status="error", result="action timed out",
                                     updated_at=time.time(), worker_active=False)
            return
        try:
            work = asyncio.create_task(asyncio.to_thread(func))
            try:
                remaining = max(0.001, deadline - (time.time() - now))
                result = await asyncio.wait_for(asyncio.shield(work), timeout=remaining)
            except asyncio.TimeoutError:
                with _JOB_LOCK:
                    _JOBS[job_id].update(status="error", result="action timed out",
                                         updated_at=time.time())
                # The worker thread cannot be killed safely. Keep holding the
                # semaphore until it exits so timeouts cannot evade the cap.
                try:
                    await work
                except Exception as exc:
                    log.error("timed-out job %s eventually failed: %s", job_id, exc)
                finally:
                    with _JOB_LOCK:
                        _JOBS[job_id].update(worker_active=False,
                                             updated_at=time.time())
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("job %s (%s) failed: %s", job_id, label, exc)
                with _JOB_LOCK:
                    _JOBS[job_id].update(status="error", result="action failed",
                                         updated_at=time.time(), worker_active=False)
                return
            with _JOB_LOCK:
                _JOBS[job_id].update(status="done", result=result,
                                     updated_at=time.time(), worker_active=False)
        finally:
            _JOB_SEMAPHORE.release()

    task = asyncio.create_task(runner())
    with _JOB_LOCK:
        _JOB_TASKS[job_id] = task
    def forget_task(_task):
        with _JOB_LOCK:
            _JOB_TASKS.pop(job_id, None)
    task.add_done_callback(forget_task)
    return {"job_id": job_id, "status": "working", "result": "working on it…"}


@app.post("/api/request")
async def request_content(body: dict):
    """Natural-language 'pull this into the rotation': a URL, 'more X cards', or a
    vibe like 'more stoner stuff'. Returns IMMEDIATELY with a job id and runs the
    work in the background — downloads/captures can take minutes and must not hold
    the HTTP connection open (that 504s behind the proxy)."""
    text = str((body or {}).get("text", ""))[:2000]
    if not text.strip():
        return {"job_id": None, "status": "done", "result": "type something to add"}
    return _start_job(text, lambda: ingest.handle(text))


@app.get("/api/request/{job_id}")
def request_status(job_id: str):
    """Poll a background ingest job: {status: working|done|error, result}.

    Jobs are in-memory: a restart loses them. Results are retained for an hour
    and the registry is capped, so treat the result as transient polling state.
    """
    _prune_jobs()
    with _JOB_LOCK:
        j = dict(_JOBS[job_id]) if job_id in _JOBS else None
    if not j:
        return JSONResponse({"status": "unknown", "result": "job not found"}, status_code=404)
    return j


@app.post("/api/sources/{action}")
async def source_action(action: str):
    """Run a source maintenance pass now.

    Actions: `capture-windows` (re-snapshot the YouTube-backed live cams) and
    `fetch-queue` (retry pending public-domain downloads). Both also run on the
    schedule in jobs.py; this exists for "do it now" from the dashboard.
    """
    scripts = {"capture-windows": "bumparr.sources.capture_windows",
               "fetch-queue": "bumparr.sources.fetch_queue"}
    mod = scripts.get(action)
    if not mod:
        return JSONResponse({"error": "unknown action"}, status_code=400)
    def work():
        r = _run(mod)
        return {"action": action, "ok": r.returncode == 0,
                "output": (r.stdout or r.stderr)[-800:]}
    return _start_job(action, work)


@app.get("/healthz")
def healthz():
    """Liveness probe for orchestrators/reverse proxies. Reaching it means the
    process is up; it deliberately does not check the DB,
    upstream sources or the pool."""
    return {"ok": True, "service": "bumparr"}


# ---------- Dashboard + media ----------

@app.get("/")
def dashboard():
    """The web dashboard (see docs/API.md, 'Dashboard'). Static assets are
    served from /web; the page itself is injected so it works with no static
    file server in front."""
    try:
        return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        log.error("dashboard read failed: %s", exc)
        return JSONResponse({"error": "dashboard unavailable"}, status_code=500)


# Source material and produced bumpers live in separate trees, so both are
# served. /bumpers is mounted first: it is Bumparr's own output and the thing
# consumers actually play, and mounting it separately means a registry uri stays
# valid no matter where the deployer points OUTPUT.
# Conformed station segments. Served straight from the cache: the playlist
# is arithmetic and the segment is a file, which is the whole point.
app.mount("/station/seg",
          _StationSegmentFiles(directory=str(config.ASSET_ROOT / ".cache" / "station"), check_dir=False),
          name="station-segments")
app.mount("/media/bumpers",
          StaticFiles(directory=str(config.OUTPUT_DIR), check_dir=False), name="produced")
app.mount("/media", StaticFiles(directory=str(config.ASSET_ROOT), check_dir=False), name="media")
app.mount("/web", StaticFiles(directory=str(WEB_DIR), check_dir=False), name="web")
