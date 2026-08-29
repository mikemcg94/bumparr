"""Capture short snippets from live webcams and store them as 'window'
clips. Runs INSIDE the container (needs yt-dlp at /opt/bin + ffmpeg on PATH).

Live googlevideo HLS can't be reliably proxied (single-use segment routing, no
CORS), so instead we grab a fresh ~20-45s snippet from each cam on a refresh
cycle and play it as an ordinary video bumper. For a "window onto somewhere"
bumper the recent-vs-live distinction is invisible, and it's rock-solid to play.

Re-run on a schedule to keep the windows current. Each cam overwrites its own
file, so storage stays flat (~11 small mp4s).
"""
import os

from bumparr import config
import random
import sqlite3
import subprocess
import time

import shutil

YTDLP = shutil.which("yt-dlp") or "yt-dlp"   # installed in the image, on PATH
OUT = config.env("WINDOWS_DIR", "/assets/windows")
DB = config.env("DB", "/data/bumparr.db")


def _probe_duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except Exception:
        return 60.0


def _upsert_window(slug, path):
    """Insert-or-refresh the window's playable row with the current file's duration,
    so random-segment playback always uses the fresh clip's real length."""
    rel = "windows/%s.mp4" % slug
    pid = "vid:" + rel
    dur = _probe_duration(path)
    try:
        c = sqlite3.connect(DB, timeout=15)
        c.execute("PRAGMA busy_timeout=8000")
        if c.execute("SELECT 1 FROM playables WHERE id=?", (pid,)).fetchone():
            c.execute("UPDATE playables SET duration=?, health='ok', enabled=1 WHERE id=?", (dur, pid))
        else:
            c.execute(
                "INSERT OR IGNORE INTO playables (id,type,kind,source,uri,duration,title,payload,tags,weight,enabled,health,last_played,play_count,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,1,'ok',0,0,?)",
                (pid, "video", "window", "youtube-live", rel, dur,
                 slug.replace("-", " ").title(), "{}", "live,window", 1.5, time.time()))
        c.commit(); c.close()
    except Exception as e:
        print("  db update err", slug, e)

# Snapshot cams come from config/live_cams.yaml (user-owned, location-bound) —
# not hardcoded, so a deployer adds their own local cams without touching code.
def _load_cams():
    # bumparr/config_files/live_cams.yaml (this file is bumparr/sources/capture_windows.py)
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config_files", "live_cams.yaml")
    try:
        import yaml
        data = yaml.safe_load(open(path).read()) or {}
        cams = [(c["slug"], c["query"]) for c in data.get("snapshot_cams", []) if c.get("slug") and c.get("query")]
        if cams:
            return cams
    except Exception as e:
        print("  cam config load error:", e)
    return []


CAMS = _load_cams()


def capture(slug, query):
    # Capture length. Live capture takes ~real-time, so keep it modest for
    # reliability (11 cams add up); random-segment still varies the shown slice
    # across the refresh cycle. Timeout must exceed the capture length + overhead,
    # or yt-dlp gets killed mid-download and leaves a ".part" that never finalizes.
    seconds = random.choice([22, 28, 35])
    dest = os.path.join(OUT, "%s.mp4" % slug)
    tmp = os.path.join(OUT, ".%s.tmp.mp4" % slug)
    # Clear any stale partials from a previously-killed run.
    for junk in (tmp, tmp + ".part"):
        try:
            if os.path.exists(junk):
                os.remove(junk)
        except Exception:
            pass
    # Two steps, both fast: (1) yt-dlp RESOLVES the live URL (~3s), (2) ffmpeg
    # stream-COPIES a slice (-c copy, no re-encode -> ~realtime, not CPU-bound).
    # The old --downloader ffmpeg path re-encoded and ran 3-5x too slow, blowing
    # the timeout and leaving an unfinalized .part.
    try:
        url = subprocess.run(
            [YTDLP, "--no-warnings", "--match-filter", "is_live", "--get-url",
             "-f", "b[height<=720]/b", "ytsearch1:" + query],
            capture_output=True, text=True, timeout=60).stdout.strip().split("\n")[0]
    except Exception as e:
        print("  resolve error", slug, e)
        return False
    if not url.startswith("http"):
        print("  no live url", slug)
        return False
    try:
        # -rw_timeout (µs) aborts a stuck segment read instead of hanging forever —
        # this is what made captures unreliable.
        #
        # NOT -c copy. Some public cam encoders are variable-frame-rate with a
        # nominal rate that does not match what they actually deliver (a real
        # example: one DOT camera declares 59.94fps while sending 15fps). Copying
        # preserves that broken timing verbatim, and since a capture becomes a
        # permanent bumper the stutter would be baked in forever. Re-encoding to
        # constant frame rate normalises it once, at capture time.
        subprocess.run(
            ["ffmpeg", "-y", "-rw_timeout", "15000000", "-i", url,
             "-t", str(seconds), "-vsync", "cfr", "-r", "30",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p", "-an", "-f", "mp4", tmp],
            capture_output=True, text=True, timeout=seconds + 180)
    except Exception as e:
        print("  ffmpeg error", slug, e)
        return False
    if os.path.exists(tmp) and os.path.getsize(tmp) > 50000:
        os.replace(tmp, dest)   # atomic swap so the player never sees a half-file
        _upsert_window(slug, dest)   # refresh the DB row's duration to the new clip
        print("  OK   %-18s %ds  %dKB" % (slug, seconds, os.path.getsize(dest) // 1024))
        return True
    print("  FAIL %-18s (no usable file — cam may be offline)" % slug)
    return False


def main():
    os.makedirs(OUT, exist_ok=True)
    ok = 0
    for slug, query in CAMS:
        if capture(slug, query):
            ok += 1
    print("captured %d/%d windows" % (ok, len(CAMS)))


if __name__ == "__main__":
    main()
