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
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from bumparr import config, paths

ROOT = config.env("ASSET_ROOT", "/assets")
QUEUE = Path(__file__).resolve().parent.parent / "config_files" / "fetch_queue.yaml"
STATE = Path(config.env("DATA_DIR", "/data")) / "fetch_done.json"
UA = {"User-Agent": "Mozilla/5.0 bumparr (polite)"}
GAP = 8
MAX_MB = 140


def _load_done():
    """Set of identifiers already downloaded, from the state file.

    A missing/corrupt state file means "retry everything", which is the safe
    direction: fetch() itself is idempotent on a present destination file.
    """
    try:
        return set(json.loads(STATE.read_text()))
    except Exception:
        return set()


def _save_done(done):
    """Persist the downloaded set; best-effort, so a failed save only costs a
    redundant re-check next pass, never a lost download."""
    try:
        STATE.write_text(json.dumps(sorted(done)))
    except Exception as e:
        print("  state save error:", e)


def gj(u):
    """GET a URL and parse the body as JSON (archive.org metadata calls)."""
    with urllib.request.urlopen(
            urllib.request.Request(u, headers=UA), timeout=45) as response:
        return json.load(response)


def pick(files):
    """The best MP4 to download from an archive.org item's file list.

    Prefers the 512kb rendition (small, plenty for bumpers, kind to the
    archive's egress) and rejects anything over MAX_MB. Returns the filename
    or None — None means "leave this on the queue and try again later".
    """
    cands = []
    for f in files:
        if not isinstance(f, dict):
            continue
        nm = f.get("name", "")
        if not isinstance(nm, str):
            continue
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
    """Download one queued item into its category, or report it as still pending.

    Every failure path returns False WITHOUT marking the item done — that is
    the self-healing: the queue simply re-attempts it next pass, which is how a
    down storage node recovers without anyone noticing. On success the
    identifier is added to `done` by the caller.
    """
    # Categories live directly under ASSET_ROOT (same as the seeder). ROOT already
    # points at the bumpers dir, so never append another "bumpers/" here.
    outdir = paths.resolve_kind_dir(ROOT, cat)
    if outdir is None:
        print("  bad category", repr(cat))
        return False
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        m = gj("https://archive.org/metadata/%s" %
               urllib.parse.quote(str(ident), safe=""))
    except Exception as e:
        print("  META FAIL", ident, e); return False
    time.sleep(GAP)
    if not isinstance(m, dict):
        print("  invalid metadata", ident)
        return False
    files = m.get("files", [])
    nm = pick(files if isinstance(files, list) else [])
    if not nm:
        print("  no mp4 under cap", ident); return False
    combined = "%s__%s" % (paths.safe_filename(ident, "item"),
                             paths.safe_filename(nm, "clip.mp4"))
    dest = outdir / paths.safe_filename(combined, "item__clip.mp4", max_bytes=180)
    if not dest.resolve().is_relative_to(outdir.resolve()):
        print("  bad path", ident)
        return False
    if dest.is_file() and dest.stat().st_size > 20000:
        done.add(ident); return True
    url = "https://archive.org/download/%s/%s" % (
        urllib.parse.quote(str(ident), safe=""),
        urllib.parse.quote(str(nm), safe=""),
    )
    partial = outdir / (".bumparr-fetch-%s.part" % uuid.uuid4().hex)
    try:
        req = urllib.request.Request(url, headers=UA)
        cap = MAX_MB * 1048576
        total = 0
        with urllib.request.urlopen(req, timeout=400) as r, partial.open("xb") as out:
            try:
                declared = int(r.headers.get("Content-Length") or 0)
            except (AttributeError, TypeError, ValueError):
                declared = 0
            if declared > cap:
                raise ValueError("declared download exceeds cap")
            while True:
                chunk = r.read(min(262144, cap + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    raise ValueError("actual download exceeds cap")
                out.write(chunk)
        if total <= 20000:
            raise ValueError("download too small")
        os.replace(partial, dest)
    except Exception as e:
        print("  still failing (node down?)", ident, str(e)[:50])
        try:
            partial.unlink()
        except OSError:
            pass
        return False
    if dest.is_file() and dest.stat().st_size > 20000:
        print("  GOT [%s] %s (%dKB)" % (cat, ident, dest.stat().st_size//1024))
        done.add(ident)
        return True
    return False


def run():
    """One pass over the fetch queue: attempt every pending item, spaced by
    GAP seconds to stay polite to archive.org. Run on the jobs.py schedule;
    each pass makes permanent progress until the queue drains."""
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
