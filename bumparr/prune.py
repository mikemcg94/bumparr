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

from bumparr import config, db, paths

VIDEO_EXT = (".mp4", ".mkv", ".webm", ".m4v", ".mov", ".avi")


def aspect(path):
    """(aspect_ratio, width, height) of a video; (None, 0, 0) if unreadable."""
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
    return paths.resolve_media(uri)


def _same_entry(path):
    """Compare-only spelling of a path: real directory, literal entry name.

    The two sides of "is this file registered?" are spelled differently on
    purpose. `paths.resolve_media` returns a lexical path so that deleting an
    in-tree symlink never follows through to its target, while
    `paths.resolve_kind_dir` returns a resolved one. That is fine until
    ASSET_ROOT is itself a symlink — `/assets -> /mnt/user/media` is an
    ordinary NAS layout — at which point the same file arrives under two names
    and a registered file reads as an unregistered leftover. Resolving only the
    directory reconciles the two spellings; keeping the last component literal
    keeps the symlink distinction the lexical path was there to protect.
    """
    p = Path(path)
    try:
        return Path(os.path.realpath(p.parent)) / p.name
    except OSError:
        return p


def _remove_registered(rows, extra_files=()):
    """Stage files, commit row removals, then purge staged files."""
    staged = []
    removed = []
    try:
        registered_paths = {_same_entry(row.get("path") or _resolve(row.get("uri") or ""))
                            for row in rows if row.get("path") or _resolve(row.get("uri") or "")}
        for extra in extra_files:
            if _same_entry(extra) in registered_paths or not extra.is_file():
                continue
            try:
                quarantined = paths.stage_delete(extra)
            except OSError as exc:
                print("    FAILED %s: staging failed (%s)" % (extra, exc))
                continue
            if quarantined is not None:
                staged.append((extra, quarantined))
        with db.conn() as c:
            for row in rows:
                path = row.get("path") or _resolve(row.get("uri") or "")
                quarantined = None
                if path is not None:
                    try:
                        quarantined = paths.stage_delete(path)
                    except OSError as exc:
                        print("    FAILED %s: staging failed (%s)" %
                              (row.get("uri", ""), exc))
                        continue
                if quarantined is not None:
                    staged.append((path, quarantined))
                c.execute("DELETE FROM playables WHERE id=?", (row["id"],))
                removed.append(row.get("uri"))
    except Exception:
        for original, quarantined in reversed(staged):
            try:
                paths.restore_delete(original, quarantined)
            except OSError as exc:
                print("    CRITICAL restore failure %s: %s" % (original, exc))
        raise
    cleanup_failed = 0
    for _, quarantined in staged:
        try:
            paths.finish_delete(quarantined)
        except OSError as exc:
            cleanup_failed += 1
            print("    quarantine retained %s: %s" % (quarantined, exc))
    return removed, cleanup_failed


def find_portrait_videos():
    """Registered videos whose shape is narrower than the policy allows."""
    hits = []
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, kind, uri FROM playables WHERE type='video' "
            "AND uri IS NOT NULL AND uri NOT LIKE 'http%'").fetchall()]
    for r in rows:
        path = _resolve(r["uri"])
        if path is None or not path.is_file():
            continue        # remote/stream, or escaping the media trees: not ours to probe
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
    category_dirs = {}
    for name in names:
        resolved = [paths.resolve_kind_dir(root, name)
                    for root in (Path(config.ASSET_ROOT), Path(config.OUTPUT_DIR))]
        if any(d is None for d in resolved):
            print("[prune] invalid category: %r" % name)
            return {"removed": 0, "dirs": 0, "error": "invalid category"}
        category_dirs[name] = resolved
    removed, dirs, cleanup_failed = [], [], 0
    for name in names:
        with db.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, uri FROM playables WHERE kind=?", (name,)).fetchall()]
        print("[prune] category %-18s %d registered entr%s"
              % (name, len(rows), "y" if len(rows) == 1 else "ies"))
        extras = [f for d in category_dirs[name] if d.is_dir()
                  for f in d.iterdir() if f.is_file()]
        if apply:
            try:
                gone, failed = _remove_registered(rows, extras)
                removed.extend(gone)
                cleanup_failed += failed
            except Exception as exc:
                print("    category transaction failed: %s" % exc)
                continue
        else:
            registered = {_same_entry(p)
                          for p in (_resolve(row["uri"] or "") for row in rows) if p}
            for row in rows:
                print("    would remove %s" % (row["uri"] or "")[:64])
            for extra in extras:
                if _same_entry(extra) not in registered:
                    print("    would remove unregistered %s" % extra)
        # Dropping a category takes its whole directory: the registered rows and
        # any unregistered leftovers were both staged above, so this rmdir is
        # clearing what is now empty. The dry run lists the leftovers by name —
        # deleting a file the operator never saw in the preview is the one
        # outcome this command must not produce.
        for d in category_dirs[name]:
            if not d.is_dir():
                continue
            if apply:
                try:
                    d.rmdir()
                    dirs.append(str(d))
                except OSError:
                    pass
            else:
                print("    would remove dir %s" % d)
    if apply:
        print("[prune] removed %d entr%s and %d director%s"
              % (len(removed), "y" if len(removed) == 1 else "ies",
                 len(dirs), "y" if len(dirs) == 1 else "ies"))
    else:
        print("[prune] DRY RUN — nothing deleted. Re-run with --apply.")
    return {"removed": len(removed), "dirs": len(dirs),
            "cleanup_failed": cleanup_failed}


def prune(apply=False, include_orphans=True):
    """Remove portrait video: registered rows (file + row) and orphan files.

    Dry run by default — this deletes files, and the per-kind summary it
    prints is how a deployer decides which categories a sweep will hurt.
    Returns counts for the caller (CLI/API) to report.
    """
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

    try:
        removed, cleanup_failed = _remove_registered(hits)
    except Exception as exc:
        print("[prune] transaction failed; registered files restored: %s" % exc)
        removed, cleanup_failed = [], 0
    deleted = len(removed)
    for o in orphans:
        try:
            o["path"].unlink()
            deleted += 1
        except Exception as e:
            print("    FAILED %s: %s" % (o["uri"], e))
    print("[prune] removed %d file(s) and their registry rows" % deleted)
    return {"registered": len(hits), "orphans": len(orphans), "deleted": deleted,
            "cleanup_failed": cleanup_failed}


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
