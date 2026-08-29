"""Self-healing archive.org fetch queue.

archive.org occasionally has a storage node down, so a verified-PD item can fail
to download for hours even though the item is fine. This queue records wanted
items and re-attempts the failures on each run until they land — so PD sourcing
is resilient like the live-cam capture, no manual re-runs.

Queue file: config/fetch_queue.yaml  (identifier + target category)
Downloaded items are marked done in a state file so they aren't re-fetched.
"""
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from bumparr import config

ROOT = config.env("ASSET_ROOT", "/assets")
QUEUE = Path(__file__).resolve().parent.parent / "config_files" / "fetch_queue.yaml"
STATE = Path(config.env("DATA_DIR", "/data")) / "fetch_done.json"
UA = {"User-Agent": "Mozilla/5.0 bumparr (polite)"}
GAP = 8
MAX_MB = 140


def _load_done():
    try:
        return set(json.loads(STATE.read_text()))
    except Exception:
        return set()


def _save_done(done):
    try:
        STATE.write_text(json.dumps(sorted(done)))
    except Exception as e:
        print("  state save error:", e)


def gj(u):
    return json.load(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=45))


def pick(files):
    cands = []
    for f in files:
        nm = f.get("name", "")
        if not nm.lower().endswith(".mp4"):
            continue
        try:
            size = int(f.get("size", 0))
        except Exception:
            size = 0
        if size and size > MAX_MB * 1048576:
            continue
        cands.append((0 if nm.endswith("_512kb.mp4") else 1, size, nm))
    cands.sort()
    return cands[0][2] if cands else None


def fetch(ident, cat, done):
    # Categories live directly under ASSET_ROOT (same as the seeder). ROOT already
    # points at the bumpers dir, so never append another "bumpers/" here.
    outdir = os.path.join(ROOT, cat)
    os.makedirs(outdir, exist_ok=True)
    try:
        m = gj("https://archive.org/metadata/%s" % ident)
    except Exception as e:
        print("  META FAIL", ident, e); return False
    time.sleep(GAP)
    nm = pick(m.get("files", []))
    if not nm:
        print("  no mp4 under cap", ident); return False
    dest = os.path.join(outdir, "%s__%s" % (ident, nm))
    if os.path.exists(dest) and os.path.getsize(dest) > 20000:
        done.add(ident); return True
    url = "https://archive.org/download/%s/%s" % (ident, urllib.parse.quote(nm))
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=400) as r, open(dest, "wb") as out:
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                out.write(chunk)
    except Exception as e:
        print("  still failing (node down?)", ident, str(e)[:50])
        try:
            if os.path.exists(dest): os.remove(dest)
        except Exception:
            pass
        return False
    if os.path.exists(dest) and os.path.getsize(dest) > 20000:
        print("  GOT [%s] %s (%dKB)" % (cat, ident, os.path.getsize(dest)//1024))
        done.add(ident)
        return True
    return False


def run():
    try:
        import yaml
        items = (yaml.safe_load(QUEUE.read_text()) or {}).get("items", [])
    except Exception as e:
        print("queue load error:", e); return
    done = _load_done()
    pending = [it for it in items if it.get("id") and it["id"] not in done]
    print("fetch queue: %d pending of %d" % (len(pending), len(items)))
    got = 0
    for it in pending:
        if fetch(it["id"], it.get("category", "misc"), done):
            got += 1
        time.sleep(GAP)
    _save_done(done)
    print("fetched %d this pass, %d still pending" % (got, len(pending) - got))


if __name__ == "__main__":
    run()
