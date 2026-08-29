"""Load user-configured live cameras from config/live_cams.yaml into the playable
registry as 'stream' items. Cams are pure config — a deployer edits the YAML to
point Bumparr at their own local/regional feeds; no code change.
"""
import hashlib
import json
import time
from pathlib import Path

from bumparr import config, db

CONFIG = Path(__file__).resolve().parent / "config_files" / "live_cams.yaml"


def load_cams():
    if not CONFIG.exists():
        return 0
    try:
        import yaml
        data = yaml.safe_load(CONFIG.read_text()) or {}
    except Exception as e:
        print("[bumparr] live_cams config error:", e)
        return 0
    cams = data.get("cams", []) or []
    added = updated = 0
    with db.conn() as c:
        for cam in cams:
            url = (cam.get("url") or "").strip()
            if not url:
                continue
            pid = "stream:cam:" + hashlib.md5(url.encode()).hexdigest()[:10]
            payload = json.dumps({
                "direct": bool(cam.get("direct", True)),
                "label": cam.get("title", "Live"),
                "region": cam.get("region", ""),
            })
            weight = float(cam.get("weight", 1.0))
            title = cam.get("title", "Live Cam")
            kind = cam.get("kind", "webcam")
            if c.execute("SELECT 1 FROM playables WHERE id=?", (pid,)).fetchone():
                c.execute("UPDATE playables SET uri=?, payload=?, weight=?, title=?, kind=?, health='ok', enabled=1 WHERE id=?",
                          (url, payload, weight, title, kind, pid))
                updated += 1
            else:
                c.execute(
                    "INSERT OR IGNORE INTO playables (id,type,kind,source,uri,duration,title,payload,tags,weight,enabled,health,last_played,play_count,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,1,'ok',0,0,?)",
                    (pid, "stream", kind, "live-cam", url, 45.0, title, payload, "live,window", weight, time.time()))
                added += 1
        c.commit()
    return added + updated
