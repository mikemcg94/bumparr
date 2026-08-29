"""Resolve curated live webcams (YouTube-live) to HLS and register them as
'stream' playables. Re-run on a schedule to refresh expiring URLs.

YouTube-live HLS manifests are IP-locked (to this WAN IP — fine, the proxy fetches
from here) and time-signed (expire in hours), so this script is both the initial
loader AND the refresher: it upserts by a stable id derived from the query, so a
re-run just updates the uri in place.

Run wherever yt-dlp and python3 are available:
    python -m bumparr.sources.resolve_cams
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time

from bumparr import config

DB = config.DB_PATH
# Resolve from PATH rather than a deployment-specific location.
YTDLP = shutil.which("yt-dlp") or "yt-dlp"

# Curated 24/7 live cams across the four vibes. Each is a search query resolved to
# whatever is currently live for it (avoids hardcoding volatile video IDs).
# (query, kind, title, weight)
CAMS = [
    # calm / scenic
    ("Jackson Hole Wyoming Town Square live cam", "webcam", "Jackson Hole, Wyoming", 1.4),
    ("Venice Italy Grand Canal live cam", "webcam", "Venice, Italy", 1.2),
    ("Waikiki Beach Honolulu live cam", "webcam", "Waikiki Beach, Hawaii", 1.2),
    # nature / wildlife
    ("explore.org African watering hole live cam", "webcam", "Watering Hole, Africa", 1.3),
    ("Monterey Bay Aquarium jelly cam live", "webcam", "Monterey Bay Aquarium", 1.2),
    ("Cornell Lab bird feeder live cam", "webcam", "Bird Feeder", 1.1),
    # urban / transit
    ("Times Square NYC live cam EarthCam", "webcam", "Times Square, NYC", 1.2),
    ("Shibuya Crossing Tokyo live cam", "webcam", "Shibuya Crossing, Tokyo", 1.3),
    ("Abbey Road crossing London live cam", "webcam", "Abbey Road, London", 1.0),
    # weird / liminal
    ("Northern Lights aurora live cam", "webcam", "Somewhere North", 1.1),
    ("Old Faithful geyser live cam", "webcam", "Old Faithful, Wyoming", 1.0),
]


def resolve(query):
    """Return a live HLS URL for the query, or None."""
    try:
        out = subprocess.run(
            [YTDLP, "--no-warnings", "--match-filter", "is_live", "--get-url",
             "-f", "b[protocol^=m3u8]/b", "ytsearch2:" + query],
            capture_output=True, text=True, timeout=90).stdout.strip()
    except Exception as e:
        print("  resolve error:", query, e)
        return None
    for line in out.splitlines():
        if line.startswith("http") and ".m3u8" in line:
            return line.strip()
    return None


def main():
    c = sqlite3.connect(DB, timeout=15)
    c.execute("PRAGMA busy_timeout=8000")
    ok = fail = 0
    for query, kind, title, weight in CAMS:
        pid = "stream:yt:" + hashlib.md5(query.encode()).hexdigest()[:10]
        url = resolve(query)
        if not url:
            # Keep the row but mark unhealthy so the scheduler skips it until next refresh.
            c.execute("UPDATE playables SET health='dead' WHERE id=?", (pid,))
            c.commit()
            print("  FAIL", title)
            fail += 1
            time.sleep(2)
            continue
        payload = json.dumps({"label": title, "source": "youtube-live", "query": query})
        # Upsert: insert if new, else refresh uri + revive health.
        exists = c.execute("SELECT 1 FROM playables WHERE id=?", (pid,)).fetchone()
        if exists:
            c.execute("UPDATE playables SET uri=?, health='ok', enabled=1, payload=? WHERE id=?",
                      (url, payload, pid))
        else:
            c.execute(
                "INSERT OR IGNORE INTO playables (id,type,kind,source,uri,duration,title,payload,tags,weight,enabled,health,last_played,play_count,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,1,'ok',0,0,?)",
                (pid, "stream", kind, "youtube-live", url, 45.0, title, payload, "live,window", weight, time.time()))
        c.commit()
        print("  OK  ", title)
        ok += 1
        time.sleep(2)
    c.close()
    print("resolved %d live cams, %d failed" % (ok, fail))


if __name__ == "__main__":
    main()
