"""In-container background jobs. Ships with Bumparr so any deployment gets them,
with no host cron and no host-specific plumbing.

Re-captures the live-window snippets so the cams stay current. A live window is
a picture of right now — the camera is worth watching precisely because it keeps
changing — so each capture REPLACES the last rather than accumulating, and a
snippet that is never refreshed is just a stale still that claims to be live.

This belongs to Bumparr, not to the player: keeping source material current is
content production. It previously lived on the player side, which meant a
standalone Bumparr deployment captured its windows once and then froze them
forever.

Staleness-aware, so a restart does not trigger a needless re-capture.
"""
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from bumparr import config

# Subprocesses must import the bumparr package regardless of cwd, so point
# PYTHONPATH at the repo root (the dir containing bumparr/) and invoke by
# module, never by file path (a path invocation puts the script's own dir on
# sys.path and the package import then fails).
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)


def _subenv():
    """Environment for child python processes: PYTHONPATH pointed at the repo
    root so `python -m bumparr...` imports the package from any cwd (the
    container's WORKDIR is not guaranteed to be the repo root)."""
    e = dict(os.environ)
    e["PYTHONPATH"] = _REPO_ROOT + os.pathsep + e.get("PYTHONPATH", "")
    return e

REFRESH_HOURS = float(config.env("WINDOW_REFRESH_HOURS", "2"))
WINDOWS_DIR = Path(config.env("WINDOWS_DIR", str(config.ASSET_ROOT / "windows")))
CAPTURE_MODULE = "bumparr.sources.capture_windows"
ENABLED = config.env("WINDOW_REFRESH", "1") not in ("0", "false", "no")


def _newest_window_age():
    """Seconds since the most recently captured window, or None if none exist."""
    try:
        mp4s = list(WINDOWS_DIR.glob("*.mp4"))
    except Exception:
        return None
    if not mp4s:
        return None
    try:
        return time.time() - max(f.stat().st_mtime for f in mp4s)
    except OSError:
        return None


def _run_capture():
    """Run the window-capture module as a child process; errors are logged,
    never raised, because a flaky capture must not kill the refresh loop."""
    try:
        subprocess.run([sys.executable, "-m", CAPTURE_MODULE], timeout=60 * 30, env=_subenv())
    except Exception as e:
        print("[bumparr] window capture error:", e)


FETCH_MODULE = "bumparr.sources.fetch_queue"


def _run_fetch_queue():
    """Retry any archive.org items still pending (self-heals node outages)."""
    try:
        subprocess.run([sys.executable, "-m", FETCH_MODULE], timeout=60 * 30, env=_subenv())
    except Exception as e:
        print("[bumparr] fetch queue error:", e)


def _apply_seasons():
    """Re-weight seasonal categories for today's date.

    Runs beside the dated-card rotation because it answers the same question in
    a different unit: that one asks which CARD belongs to today, this asks which
    CATEGORY belongs to this part of the year.
    """
    # Nothing to do at runtime any more: seasonal factors are computed during
    # scoring rather than written into the pool. This now only heals rows left
    # weighted by the older, destructive version.
    try:
        from bumparr import seasons
        n = seasons.restore_base_weights()
        if n:
            print("[bumparr] restored %d declared weight(s) from the old seasonal pass" % n)
    except Exception as e:
        print("[bumparr] seasonal restore error: %s" % e)


def _rotate_dated_cards():
    """Bring today's date-bound cards into rotation and park the rest.

    Cheap and idempotent, so it can run on startup and then daily. Without it a
    channel left running across midnight keeps airing yesterday's "on this day",
    which is the one way these cards can be plainly wrong.
    """
    try:
        from bumparr import db
        from bumparr.generators import on_this_day
        with db.conn() as c:
            on, off = on_this_day.retire_other_days(c)
            c.commit()
        if on or off:
            print("[bumparr] dated cards: %d in, %d parked" % (on, off))
    except Exception as e:
        print("[bumparr] dated-card rotation error: %s" % e)


async def _dated_once():
    """One dated-card rotation + seasonal restore pass, each guarded independently.

    Errors are logged, never raised, so a startup DB lock cannot kill the loop.
    Only CancelledError exits.
    """
    try:
        await asyncio.to_thread(_rotate_dated_cards)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("[bumparr] dated-card loop error: %s" % e)
    try:
        await asyncio.to_thread(_apply_seasons)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("[bumparr] dated-card loop error: %s" % e)


async def dated_card_loop():
    """Re-check date-bound cards hourly, so a midnight rollover is caught."""
    import asyncio
    await _dated_once()
    while True:
        try:
            await asyncio.sleep(3600)
            await _dated_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[bumparr] dated-card loop error: %s" % e)


async def _refresh_once(initial=False):
    """One window-refresh cycle: capture (staleness-aware on initial) then fetch queue.

    The two operations are guarded independently so one capture failure never
    prevents the fetch-queue pass in the same cycle. Only CancelledError exits.
    """
    if initial:
        interval = REFRESH_HOURS * 3600
        # On startup, only capture if the windows are missing or already stale.
        age = _newest_window_age()
        if age is None or age > interval:
            print("[bumparr] windows missing/stale -> initial capture")
            try:
                await asyncio.to_thread(_run_capture)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print("[bumparr] window capture error: %s" % e)
        try:
            await asyncio.to_thread(_run_fetch_queue)   # try any pending PD downloads
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[bumparr] fetch queue error: %s" % e)
        return
    print("[bumparr] scheduled window refresh")
    try:
        await asyncio.to_thread(_run_capture)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("[bumparr] window capture error: %s" % e)
    try:
        await asyncio.to_thread(_run_fetch_queue)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("[bumparr] fetch queue error: %s" % e)


async def window_refresh_loop():
    """Scheduled re-capture of live-window snippets plus fetch-queue passes.

    Cadence is WINDOW_REFRESH_HOURS; the initial capture on startup is
    staleness-aware (see _newest_window_age) so a restart of a fresh pool does
    not re-download every cam for nothing. Disabled entirely with
    WINDOW_REFRESH=0 for deployments that capture elsewhere.
    """
    if not ENABLED:
        print("[bumparr] window refresh disabled")
        return
    interval = REFRESH_HOURS * 3600
    try:
        await _refresh_once(initial=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("[bumparr] window refresh error: %s" % e)
    while True:
        try:
            await asyncio.sleep(interval)
            await _refresh_once(initial=False)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[bumparr] window refresh error: %s" % e)


async def station_conform_loop():
    """Keep the station's segment cache in step with the pool.

    Runs a sweep on startup and then every STATION_CONFORM_INTERVAL seconds.
    Nothing in this loop is in the playback path; the channels serve
    whatever is conformed and simply gain items as the sweep lands them.
    """
    from bumparr import config
    from bumparr.station import conform, playout
    interval = config.STATION_CONFORM_INTERVAL
    if interval <= 0:
        print("[station] conform loop disabled")
        return
    while True:
        try:
            stats = await asyncio.to_thread(conform.sweep, None, playout.active_keys())
            if stats.get("conformed") or stats.get("failed") or stats.get("pruned"):
                print("[station] conform: %s" % stats)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[station] conform loop error: %s" % e)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
