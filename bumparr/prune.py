"""Remove source material that does not belong in a broadcast pool.

Currently: phone-format vertical video. A channel is a landscape medium, and a
9:16 clip never looks like broadcast however it is framed — blur-filling the
sides makes it tolerable, not right. Portrait STILLS are a different matter and
are deliberately untouched: a wartime poster is portrait because posters are,
and it reads correctly on screen.

Defaults to a dry run, because this deletes files.
"""
import argparse
import os
import subprocess
from pathlib import Path

from bumparr import config, db

VIDEO_EXT = (".mp4", ".mkv", ".webm", ".m4v", ".mov", ".avi")


def aspect(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60).stdout.strip()
        w, h = [int(x) for x in out.split(",")[:2]]
        return (w / float(h), w, h) if h else (None, 0, 0)
    except Exception:
        return (None, 0, 0)


def _resolve(uri):
    """Registry uri -> file on disk, across the source and output trees."""
    if uri.startswith("bumpers/"):
        return Path(config.OUTPUT_DIR) / uri[len("bumpers/"):]
    return Path(config.ASSET_ROOT) / uri


def find_portrait_videos():
    """Registered videos whose shape is narrower than the policy allows."""
    hits = []
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, kind, uri FROM playables WHERE type='video' "
            "AND uri IS NOT NULL AND uri NOT LIKE 'http%'").fetchall()]
    for r in rows:
        path = _resolve(r["uri"])
        if not path.is_file():
            continue
        ar, w, h = aspect(path)
        if ar is not None and ar < config.MIN_VIDEO_ASPECT:
            hits.append({"id": r["id"], "kind": r["kind"], "uri": r["uri"],
                         "path": path, "aspect": round(ar, 3), "w": w, "h": h})
    return hits


def find_orphan_files():
    """Portrait video files on disk that no registry row references.

    A pruned source can leave its file behind if the row was removed first, and
    an unreferenced vertical clip would be picked straight back up by the next
    asset scan.
    """
    with db.conn() as c:
        known = {r[0] for r in c.execute(
            "SELECT uri FROM playables WHERE uri IS NOT NULL")}
    out = []
    root = Path(config.ASSET_ROOT)
    for f in root.rglob("*"):
        if f.suffix.lower() not in VIDEO_EXT or not f.is_file():
            continue
        rel = str(f.relative_to(root))
        if rel in known:
            continue
        ar, w, h = aspect(f)
        if ar is not None and ar < config.MIN_VIDEO_ASPECT:
            out.append({"uri": rel, "path": f, "aspect": round(ar, 3), "w": w, "h": h})
    return out


def drop_categories(names, apply=False):
    """Remove whole categories, files and rows together.

    Search-driven ingest sometimes returns material that has nothing to do with
    the request — a query for a cartoon title pulls generic stock that merely
    shares a keyword. Those categories are worse than empty, because they
    promise a theme the footage does not deliver, so removing the category
    outright is the right correction rather than weeding it clip by clip.
    """
    removed, dirs = [], []
    with db.conn() as c:
        for name in names:
            rows = [dict(r) for r in c.execute(
                "SELECT id, uri FROM playables WHERE kind=?", (name,)).fetchall()]
            print("[prune] category %-18s %d registered entr%s"
                  % (name, len(rows), "y" if len(rows) == 1 else "ies"))
            for r in rows:
                path = _resolve(r["uri"] or "")
                if apply:
                    try:
                        if path.is_file():
                            path.unlink()
                        c.execute("DELETE FROM playables WHERE id=?", (r["id"],))
                        removed.append(r["uri"])
                    except Exception as e:
                        print("    FAILED %s: %s" % (r["uri"], e))
                else:
                    print("    would remove %s" % (r["uri"] or "")[:64])
            # The source directory goes too, or the next asset scan re-seeds
            # whatever is left sitting in it.
            for root in (Path(config.ASSET_ROOT), Path(config.OUTPUT_DIR)):
                d = root / name
                if not d.is_dir():
                    continue
                if apply:
                    for f in list(d.iterdir()):
                        try:
                            f.unlink()
                        except Exception:
                            pass
                    try:
                        d.rmdir()
                        dirs.append(str(d))
                    except Exception as e:
                        print("    dir not removed %s: %s" % (d, e))
                else:
                    print("    would remove dir %s (%d file(s))" % (d, len(list(d.iterdir()))))
        if apply:
            c.commit()
    if apply:
        print("[prune] removed %d entr%s and %d director%s"
              % (len(removed), "y" if len(removed) == 1 else "ies",
                 len(dirs), "y" if len(dirs) == 1 else "ies"))
    else:
        print("[prune] DRY RUN — nothing deleted. Re-run with --apply.")
    return {"removed": len(removed), "dirs": len(dirs)}


def prune(apply=False, include_orphans=True):
    hits = find_portrait_videos()
    orphans = find_orphan_files() if include_orphans else []

    by_kind = {}
    for h in hits:
        by_kind[h["kind"]] = by_kind.get(h["kind"], 0) + 1

    print("[prune] portrait videos registered: %d (threshold aspect < %.2f)"
          % (len(hits), config.MIN_VIDEO_ASPECT))
    for k in sorted(by_kind, key=lambda k: -by_kind[k]):
        print("    %-24s %d" % (k, by_kind[k]))
    if orphans:
        print("[prune] portrait video files not in the registry: %d" % len(orphans))

    if not apply:
        print("[prune] DRY RUN — nothing deleted. Re-run with --apply.")
        return {"registered": len(hits), "orphans": len(orphans), "deleted": 0}

    deleted = 0
    with db.conn() as c:
        for h in hits:
            try:
                if h["path"].is_file():
                    h["path"].unlink()
                c.execute("DELETE FROM playables WHERE id=?", (h["id"],))
                deleted += 1
            except Exception as e:
                print("    FAILED %s: %s" % (h["uri"], e))
        c.commit()
    for o in orphans:
        try:
            o["path"].unlink()
            deleted += 1
        except Exception as e:
            print("    FAILED %s: %s" % (o["uri"], e))
    print("[prune] removed %d file(s) and their registry rows" % deleted)
    return {"registered": len(hits), "orphans": len(orphans), "deleted": deleted}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Prune material unsuited to a broadcast pool.")
    ap.add_argument("--apply", action="store_true", help="actually delete. Irreversible.")
    ap.add_argument("--skip-orphans", action="store_true")
    ap.add_argument("--drop-category", action="append", dest="drop",
                    help="remove a whole category (repeatable), e.g. a search that "
                         "returned material unrelated to the request")
    a = ap.parse_args()
    db.init_db()
    if a.drop:
        drop_categories(a.drop, apply=a.apply)
    else:
        prune(apply=a.apply, include_orphans=not a.skip_orphans)
