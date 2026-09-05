"""Populate the playable registry from assets on disk.

Video bumpers are discovered by scanning ASSET_ROOT (organised by category
sub-directory: ambient/, station_ids/, test_patterns/, ephemeral/). Card and
stream playables are added by their own generators/adapters, not here.
"""
import subprocess
from pathlib import Path

from bumparr import config, db, paths

VIDEO_EXT = {".mp4", ".webm", ".ogv", ".m4v", ".mkv"}

# Directories under ASSET_ROOT that are Bumparr's own OUTPUT or working files,
# never source material. Rendered cards live beside the fetched video but are
# already registered as card playables, so seeding them would enter every card
# a second time as an anonymous video and double its share of the pool.
SKIP_DIRS = {"cards", ".cache"}

# Map an asset sub-directory to a (kind, source, base_weight).
CATEGORY = {
    "ambient": ("ambient", "nasa", 1.2),
    "station_ids": ("station_id", "archive", 1.0),
    "test_patterns": ("testpattern", "archive", 0.6),
    "ephemeral": ("ephemeral", "archive", 0.8),
    "gm_films": ("gm_film", "archive", 0.7),
    "vintage_ads": ("vintage_ad", "archive", 0.7),
    "windows": ("window", "youtube-live", 1.5),
    "atomic_era": ("atomic_era", "archive", 0.8),
    "sports": ("sports", "archive", 0.9),
    "weather": ("weather_footage", "archive", 0.9),
    "military": ("military", "archive", 0.7),
    "cartoons": ("cartoon", "archive", 1.0),
    "trippy": ("trippy", "archive", 0.8),
    "fun_animation": ("fun_animation", "archive", 0.9),
    "automotive": ("automotive", "archive", 0.8),
}


def _probe_duration(path):
    """Positive duration via ffprobe, or None for unreadable media."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        d = float(out)
        return d if d > 0 else None
    except Exception:
        return None


def seed_from_assets():
    """Scan ASSET_ROOT and register every video file as a playable.

    Idempotent (id is the relative path, upsert_playable ignores existing
    rows), so it is safe to run on every startup and after every download.
    Category directory -> (kind, source, weight) via the CATEGORY map; an
    unknown directory becomes its own self-describing kind. Returns the number
    of newly registered files.
    """
    root = config.ASSET_ROOT
    if not root.exists():
        return 0
    added = skipped = 0
    with db.conn() as c:
        registered = {r[0] for r in c.execute("SELECT id FROM playables").fetchall()}
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXT:
                continue
            rel = str(path.relative_to(root))
            if Path(rel).parts[0] in SKIP_DIRS:
                continue
            if "vid:" + rel in registered:
                continue
            if path.parent == root:
                kind, source, weight = "unsorted", "user-added", 0.9
            else:
                category = path.parent.name
                # Known category -> its (kind, source, weight); unknown (e.g. a category
                # created by a natural-language request) -> use the folder name as the kind
                # so it's self-describing rather than mislabeled "ambient".
                kind, source, weight = CATEGORY.get(category, (category, "user-added", 0.9))
            duration = _probe_duration(path)
            if duration is None:
                skipped += 1
                continue
            row = {
                "id": "vid:" + rel,
                "type": "video",
                "kind": kind,
                "source": source,
                "uri": rel,
                "duration": duration,
                "title": path.stem.replace("_", " ").replace("~", " ").strip(),
                "weight": weight,
            }
            if db.upsert_playable(c, row):
                added += 1
        parked = cleared = 0
        for r in c.execute(
                "SELECT id, type, uri, enabled FROM playables WHERE uri IS NOT NULL").fetchall():
            uri = r["uri"]
            if (not uri or r["type"] == "stream"
                    or uri.startswith(("http://", "https://"))):
                continue
            path = paths.resolve_media(uri)
            if path is None or path.is_file():
                continue
            if r["type"] == "card":
                c.execute("UPDATE playables SET uri=NULL WHERE id=?", (r["id"],))
                cleared += 1
            elif r["type"] in ("video", "image"):
                c.execute("UPDATE playables SET enabled=0, health='dead' WHERE id=?",
                          (r["id"],))
                parked += 1
        c.commit()
    print("[seed] %d new, %d unreadable skipped, %d parked, %d card renders cleared"
          % (added, skipped, parked, cleared))
    return added
