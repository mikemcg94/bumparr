"""Load user-configured live cameras from config/live_cams.yaml into the playable
registry as 'stream' items. Cams are pure config — a deployer edits the YAML to
point Bumparr at their own local/regional feeds; no code change.
"""
import hashlib
import json
import math
import time
from pathlib import Path

from bumparr import db, paths

CONFIG = Path(__file__).resolve().parent / "config_files" / "live_cams.yaml"


def load_cams():
    """Upsert every cam from config_files/live_cams.yaml into the registry.

    Idempotent by configured id: an existing row gets its url, weight, title,
    kind and payload refreshed; a new one is inserted enabled. Refreshing an
    existing row never touches `enabled` — that column is operator intent, so a
    cam switched off stays off across reloads — and resets `health='ok'` only
    when the url actually changed, so an unchanged dead feed stays dead instead
    of being revived on every boot. Cams dropped from the YAML are parked
    (enabled=0), not deleted: history matters. Turning a parked cam back on is a
    deliberate act, POST /api/pool/enable, because re-adding it here cannot tell
    an operator's "off" from this pass's.

    Runs on startup; returns the number of rows added+updated.
    """
    if not CONFIG.exists():
        return 0
    try:
        import yaml
        data = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print("[bumparr] live_cams config error:", e)
        return 0
    if not isinstance(data, dict):
        print("[bumparr] live_cams config error: top level must be a mapping")
        return 0
    cams = data.get("cams", []) or []
    if not isinstance(cams, list):
        print("[bumparr] live_cams config error: 'cams' must be a list")
        return 0
    added = updated = 0
    pids = set()
    with db.conn() as c:
        for cam in cams:
            try:
                if not isinstance(cam, dict):
                    continue
                url = cam.get("url")
                if not isinstance(url, str) or not url.strip():
                    continue
                url = url.strip()
                raw_id = cam.get("id", cam.get("slug"))
                if raw_id is None:
                    print("[bumparr] live_cams warning: add an id; using legacy URL identity")
                    safe_id = hashlib.md5(url.encode()).hexdigest()[:10]
                elif not isinstance(raw_id, str) or not raw_id.strip():
                    print("[bumparr] live_cams skipping entry with invalid id")
                    continue
                else:
                    safe_id = paths.safe_filename(raw_id, "").lower()
                    if not safe_id:
                        print("[bumparr] live_cams skipping entry with invalid id")
                        continue
                pid = "stream:cam:" + safe_id
                if pid in pids:
                    print("[bumparr] live_cams skipping duplicate id: %s" % pid)
                    continue
                pids.add(pid)
                proxy_hosts = cam.get("proxy_hosts", [])
                if not isinstance(proxy_hosts, list):
                    print("[bumparr] live_cams proxy_hosts must be a list for %s" % pid)
                    proxy_hosts = []
                proxy_hosts = [v.strip() for v in proxy_hosts
                               if isinstance(v, str) and v.strip()]
                title = str(cam.get("title") or "Live Cam").strip() or "Live Cam"
                kind = str(cam.get("kind") or "webcam").strip() or "webcam"
                region = str(cam.get("region") or "").strip()
                payload = json.dumps({
                    "direct": bool(cam.get("direct", True)),
                    "label": title,
                    "region": region,
                    "proxy_hosts": proxy_hosts,
                })
                try:
                    weight = float(cam.get("weight", 1.0))
                except (ValueError, TypeError):
                    print("[bumparr] live_cams bad weight for %s, using 1.0" % url)
                    weight = 1.0
                if not math.isfinite(weight):
                    print("[bumparr] live_cams bad weight for %s, using 1.0" % url)
                    weight = 1.0
                row = c.execute("SELECT uri, type, source FROM playables WHERE id=?", (pid,)).fetchone()
                if row:
                    if row["type"] != "stream" or row["source"] != "live-cam":
                        print("[bumparr] live_cams id conflicts with a non-cam row: %s" % pid)
                        pids.discard(pid)
                        continue
                    if row["uri"] != url:
                        c.execute("UPDATE playables SET uri=?, payload=?, weight=?, title=?, kind=?, health='ok' WHERE id=?",
                                  (url, payload, weight, title, kind, pid))
                    else:
                        c.execute("UPDATE playables SET uri=?, payload=?, weight=?, title=?, kind=? WHERE id=?",
                                  (url, payload, weight, title, kind, pid))
                    updated += 1
                else:
                    cursor = c.execute(
                        "INSERT OR IGNORE INTO playables (id,type,kind,source,uri,duration,title,payload,tags,weight,enabled,health,last_played,play_count,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,1,'ok',0,0,?)",
                        (pid, "stream", kind, "live-cam", url, 45.0, title, payload, "live,window", weight, time.time()))
                    added += int(cursor.rowcount > 0)
            except Exception as e:
                print("[bumparr] live_cams skipping entry: %s" % e)
                continue
        if pids:
            parked = c.execute(
                "UPDATE playables SET enabled=0 WHERE source='live-cam' AND enabled!=0 AND id NOT IN (%s)"
                % ",".join("?" * len(pids)), tuple(pids))
        else:
            parked = c.execute("UPDATE playables SET enabled=0 WHERE source='live-cam' AND enabled!=0")
        if parked.rowcount:
            print("[bumparr] live_cams parked %d removed cam(s)" % parked.rowcount)
        c.commit()
    return added + updated
