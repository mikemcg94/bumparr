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
    return time.time() - max(f.stat().st_mtime for f in mp4s)


def _run_capture():
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


async def dated_card_loop():
    """Re-check date-bound cards hourly, so a midnight rollover is caught."""
    import asyncio
    await asyncio.to_thread(_rotate_dated_cards)
    await asyncio.to_thread(_apply_seasons)
    while True:
        try:
            await asyncio.sleep(3600)
            await asyncio.to_thread(_rotate_dated_cards)
            await asyncio.to_thread(_apply_seasons)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[bumparr] dated-card loop error: %s" % e)


async def window_refresh_loop():
    if not ENABLED:
        print("[bumparr] window refresh disabled")
        return
    interval = REFRESH_HOURS * 3600
    # On startup, only capture if the windows are missing or already stale.
    age = _newest_window_age()
    if age is None or age > interval:
        print("[bumparr] windows missing/stale -> initial capture")
        await asyncio.to_thread(_run_capture)
    await asyncio.to_thread(_run_fetch_queue)   # try any pending PD downloads
    while True:
        await asyncio.sleep(interval)
        print("[bumparr] scheduled window refresh")
        await asyncio.to_thread(_run_capture)
        await asyncio.to_thread(_run_fetch_queue)
