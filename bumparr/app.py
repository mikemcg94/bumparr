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
import json
import re
import os
import random
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from bumparr import config, db, seed, live_cams, stream_proxy, ingest

WEB_DIR = Path(__file__).resolve().parent / "web"


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
    import asyncio
    db.init_db()
    added = seed.seed_from_assets()
    base = ingest.register_all_baselines()
    cams = live_cams.load_cams()
    print(f"[bumparr] startup: {added} new bumper(s) seeded, {base} baseline card(s), {cams} live cam(s) loaded")
    from bumparr import jobs
    tasks = [asyncio.create_task(_volatile_refresh_loop()),
             asyncio.create_task(jobs.window_refresh_loop()),
             asyncio.create_task(jobs.dated_card_loop())]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()


app = FastAPI(title="Bumparr", lifespan=lifespan)
app.include_router(stream_proxy.router)


# ---------- Status / pool inspection ----------

@app.get("/api/status")
def status():
    with db.conn() as c:
        rows = c.execute("SELECT type, kind, source, COUNT(*) n, SUM(CASE WHEN enabled=1 AND health='ok' THEN 1 ELSE 0 END) live "
                         "FROM playables GROUP BY type, kind").fetchall()
    by_type, by_kind = {}, {}
    total = live = 0
    for r in rows:
        by_type[r["type"]] = by_type.get(r["type"], 0) + r["n"]
        by_kind[r["kind"]] = r["n"]
        total += r["n"]
        live += r["live"]
    return {"brand": config.BRAND, "total": total, "playable_now": live,
            "by_type": by_type, "by_kind": by_kind}


@app.get("/api/bumpers")
def list_bumpers(request: Request, type: str = None, kind: str = None, limit: int = 200, offset: int = 0):
    q = "SELECT * FROM playables WHERE 1=1"
    args = []
    if type:
        q += " AND type=?"; args.append(type)
    if kind:
        q += " AND kind=?"; args.append(kind)
    q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"; args += [limit, offset]
    with db.conn() as c:
        rows = c.execute(q, args).fetchall()
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


_LOOPBACK_WARNED = False


def _public_base(request):
    """Base URL external consumers should use to reach this Bumparr.

    Explicit config wins (needed behind a reverse proxy, where the app cannot
    see its own public hostname). Otherwise derive it from the request, which is
    correct for direct access on a LAN port.

    The derived form mirrors whoever asked, which is right when the fetcher and
    the player are the same machine and silently wrong when they are not: fetch
    the playlist over loopback and every entry says 127.0.0.1, which no other
    host or container can play. That failure is invisible -- the playlist looks
    fine and simply does not work -- so warn once rather than let a deployer
    discover it through a dead channel.
    """
    if config.PUBLIC_BASE_URL:
        return config.PUBLIC_BASE_URL
    if not request:
        return ""
    base = str(request.base_url).rstrip("/")
    global _LOOPBACK_WARNED
    if not _LOOPBACK_WARNED and re.search(r"//(127\.0\.0\.1|localhost|\[::1\])\b", base):
        _LOOPBACK_WARNED = True
        print("[bumparr] WARNING: handing out %s URLs, derived from a loopback "
              "request. Anything else -- another container, another host, a "
              "player -- cannot reach those. Set PUBLIC_URL to the address your "
              "consumers actually use." % base)
    return base


def _absolutize(url, request):
    """Make a Bumparr-relative URL absolute.

    Every URL Bumparr hands out is consumed by something else — ErsatzTV,
    Dispatcharr, Tunarr, Jellyfin, VLC, or a player. None of them can resolve a
    bare path, and an M3U in particular has no base-URL rule, so relative
    entries are simply unplayable. Upstream URLs that are already absolute
    (direct live-cam streams) pass through untouched.
    """
    if not url or url.startswith(("http://", "https://")):
        return url
    return _public_base(request) + url


def _media_url(row, request=None):
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
                   max_duration: float = Query(None),
                   types: str = Query(None, description="comma list, e.g. video,card")):
    """The output contract for channel generators: hand me N bumpers (optionally
    capped by duration / restricted to types) and I return playable items."""
    type_filter = set(types.split(",")) if types else None
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM playables WHERE enabled=1 AND health='ok'").fetchall()]
    pool = []
    for r in rows:
        if type_filter and r["type"] not in type_filter:
            continue
        # Zero weight means seasonally gated off the air, not merely unlikely.
        if (r["weight"] or 0) <= 0:
            continue
        if max_duration and (r["duration"] or 0) > max_duration and r["type"] != "video":
            continue
        pool.append(r)
    if not pool:
        return {"count": 0, "bumpers": []}
    # Same rotation model the player uses, so a channel generator pulling from
    # here gets the same variety rather than a naive weighted shuffle.
    from bumparr import rotation, seasons
    try:
        season = seasons.factors_now()
    except Exception:
        season = {}
    weights, _ = rotation.weights_for(pool, season)
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
         seconds: float = Query(..., gt=0, description="the gap to fill"),
         tolerance: float = Query(1.5, ge=0, description="acceptable over/under, seconds"),
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
    parser. Newlines and commas get the same treatment because #EXTINF is a
    single line whose display name runs to end-of-line.
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
    return subprocess.run([sys.executable, "-m", mod, *args],
                          capture_output=True, text=True, timeout=60 * 30)


def _resolve_media(uri):
    """Registry uri -> file on disk, across the source and output trees."""
    if not uri or uri.startswith(("http://", "https://")):
        return None
    if uri.startswith("bumpers/"):
        return Path(config.OUTPUT_DIR) / uri[len("bumpers/"):]
    return Path(config.ASSET_ROOT) / uri


@app.delete("/api/bumpers/{bumper_id:path}")
def delete_bumper(bumper_id: str, keep_file: bool = False):
    """Remove one bumper: its registry row and, by default, its file.

    Deleting the row alone is not enough for anything that came from disk — the
    asset scan would find the orphaned file on the next restart and register it
    right back. So the file goes too unless explicitly kept. Live streams have
    no local file and only lose their row.
    """
    with db.conn() as c:
        row = c.execute("SELECT id, type, uri, kind, title FROM playables WHERE id=?",
                        (bumper_id,)).fetchone()
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        uri = row["uri"] or ""
        path = _resolve_media(uri)
        file_removed = False
        if path is not None and not keep_file:
            try:
                if path.is_file():
                    path.unlink()
                    file_removed = True
            except Exception as e:
                return JSONResponse({"error": "could not remove file: %s" % e},
                                    status_code=500)
        c.execute("DELETE FROM playables WHERE id=?", (bumper_id,))
        c.commit()
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
            "file_removed": file_removed, "dir_removed": dir_removed}


@app.delete("/api/pool/kind/{kind}")
def delete_kind(kind: str, keep_files: bool = False):
    """Remove a whole category — the usual fix when a search returned junk.

    Also removes the now-empty source directory, since leaving it means the next
    asset scan re-registers anything still sitting inside it.
    """
    removed, failed = 0, []
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, uri FROM playables WHERE kind=?", (kind,)).fetchall()]
        for r in rows:
            path = _resolve_media(r["uri"] or "")
            try:
                if path is not None and not keep_files and path.is_file():
                    path.unlink()
                c.execute("DELETE FROM playables WHERE id=?", (r["id"],))
                removed += 1
            except Exception as e:
                failed.append("%s: %s" % (r["uri"], e))
        c.commit()
    dirs = 0
    if not keep_files:
        for root in (Path(config.ASSET_ROOT), Path(config.OUTPUT_DIR)):
            d = root / kind
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
    """Restore assets marked dead whose media is actually fine.

    Playback failures are reported by a player and can be transient — a blocked
    autoplay or a throttled tab looks exactly like a broken file. This re-checks
    each retired asset against reality: a local file that ffprobe can still read
    is put back into rotation. Files that are genuinely gone or unreadable stay
    retired, and live streams are left alone because their health depends on the
    far end, not on anything we can verify from here.
    """
    import shlex
    restored, still_dead, skipped = [], [], []
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, type, uri FROM playables WHERE health='dead'").fetchall()]
    for r in rows:
        uri = r["uri"] or ""
        if r["type"] == "stream" or uri.startswith(("http://", "https://")):
            skipped.append(r["id"])
            continue
        path = Path(config.OUTPUT_DIR) / uri[len("bumpers/"):] if uri.startswith("bumpers/") \
            else Path(config.ASSET_ROOT) / uri
        if not path.is_file() or path.stat().st_size == 0:
            still_dead.append(r["id"])
            continue
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True)
        if probe.returncode == 0 and probe.stdout.strip():
            restored.append(r["id"])
        else:
            still_dead.append(r["id"])
    if restored and not dry_run:
        with db.conn() as c:
            c.executemany("UPDATE playables SET health='ok', fail_count=0 WHERE id=?",
                          [(i,) for i in restored])
            c.commit()
    del shlex
    return {"checked": len(rows), "restored": len(restored),
            "still_dead": len(still_dead), "skipped_streams": len(skipped),
            "dry_run": dry_run}


@app.post("/api/starter")
def starter(dry_run: bool = False, only_free: bool = False, limit: int = None):
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
    r = _run("bumparr.starter", *args)
    return {"ok": r.returncode == 0,
            "stdout": (r.stdout or "")[-6000:], "stderr": (r.stderr or "")[-1500:]}


@app.post("/api/render/cards")
def render_cards(limit: int = None, force: bool = False):
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
    r = _run("bumparr.render_cards", *args)
    return {"ok": r.returncode == 0,
            "stdout": (r.stdout or "")[-4000:], "stderr": (r.stderr or "")[-2000:]}


@app.post("/api/generate/{kind}")
def generate(kind: str, n: int = 20):
    """Generate cards. Grounded kinds (trivia, fun_facts, number) use real
    sources; the absurd kinds (psa, etc.) use the local model."""
    grounded = {"trivia", "fun_facts", "number"}
    model_kinds = {"psa", "corrections", "achievements",
                   "coming_up", "tiny_games"}
    try:
        if kind in grounded:
            r = _run("bumparr.generators.grounded", "--kind", kind, "--n", str(n))
        elif kind == "on_this_day":
            r = _run("bumparr.generators.on_this_day", "--n", str(n))
        elif kind == "weather":
            r = _run("bumparr.generators.weather")
        elif kind in model_kinds:
            r = _run("bumparr.generators.cards", "--kind", kind, "--n", str(n))
        else:
            return JSONResponse({"error": "unknown kind: %s" % kind}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"kind": kind, "output": (r.stdout or r.stderr)[-500:]}


_JOBS = {}  # in-memory job registry (single uvicorn worker); pruned on insert


@app.post("/api/request")
async def request_content(body: dict):
    """Natural-language 'pull this into the rotation': a URL, 'more X cards', or a
    vibe like 'more stoner stuff'. Returns IMMEDIATELY with a job id and runs the
    work in the background — downloads/captures can take minutes and must not hold
    the HTTP connection open (that 504s behind the proxy)."""
    import asyncio
    import uuid
    from bumparr import ingest
    text = (body or {}).get("text", "")
    if not text.strip():
        return {"job_id": None, "status": "done", "result": "type something to add"}
    # prune finished jobs so the dict can't grow unbounded
    for jid in [k for k, v in list(_JOBS.items()) if v.get("status") != "working"][:-50]:
        _JOBS.pop(jid, None)
    job_id = uuid.uuid4().hex[:12]
    _JOBS[job_id] = {"status": "working", "result": None, "request": text}

    async def _run():
        try:
            msg = await asyncio.to_thread(ingest.handle, text)
            _JOBS[job_id] = {"status": "done", "result": msg, "request": text}
        except Exception as e:
            _JOBS[job_id] = {"status": "error", "result": str(e), "request": text}

    asyncio.create_task(_run())
    return {"job_id": job_id, "status": "working", "result": "working on it…"}


@app.get("/api/request/{job_id}")
def request_status(job_id: str):
    j = _JOBS.get(job_id)
    if not j:
        return JSONResponse({"status": "unknown", "result": "job not found"}, status_code=404)
    return j


@app.post("/api/sources/{action}")
def source_action(action: str):
    scripts = {"capture-windows": "bumparr.sources.capture_windows",
               "fetch-queue": "bumparr.sources.fetch_queue"}
    mod = scripts.get(action)
    if not mod:
        return JSONResponse({"error": "unknown action"}, status_code=400)
    try:
        r = _run(mod)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"action": action, "output": (r.stdout or r.stderr)[-800:]}


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "bumparr"}


# ---------- Dashboard + media ----------

@app.get("/")
def dashboard():
    return HTMLResponse((WEB_DIR / "index.html").read_text())


# Source material and produced bumpers live in separate trees, so both are
# served. /bumpers is mounted first: it is Bumparr's own output and the thing
# consumers actually play, and mounting it separately means a registry uri stays
# valid no matter where the deployer points OUTPUT.
app.mount("/media/bumpers",
          StaticFiles(directory=str(config.OUTPUT_DIR), check_dir=False), name="produced")
app.mount("/media", StaticFiles(directory=str(config.ASSET_ROOT), check_dir=False), name="media")
app.mount("/web", StaticFiles(directory=str(WEB_DIR), check_dir=False), name="web")
