"""Populate the playable registry from assets on disk.

Video bumpers are discovered by scanning ASSET_ROOT (organised by category
sub-directory: ambient/, station_ids/, test_patterns/, ephemeral/). Card and
stream playables are added by their own generators/adapters, not here.
"""
import subprocess
import time
from pathlib import Path

from bumparr import config, db

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
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        d = float(out)
        return d if d > 0 else config.DEFAULT_VIDEO_DURATION
    except Exception:
        return config.DEFAULT_VIDEO_DURATION


def seed_from_assets():
    root = config.ASSET_ROOT
    if not root.exists():
        return 0
    added = 0
    with db.conn() as c:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXT:
                continue
            rel = str(path.relative_to(root))
            if Path(rel).parts[0] in SKIP_DIRS:
                continue
            category = path.parent.name
            # Known category -> its (kind, source, weight); unknown (e.g. a category
            # created by a natural-language request) -> use the folder name as the kind
            # so it's self-describing rather than mislabeled "ambient".
            kind, source, weight = CATEGORY.get(category, (category, "user-added", 0.9))
            row = {
                "id": "vid:" + rel,
                "type": "video",
                "kind": kind,
                "source": source,
                "uri": rel,
                "duration": _probe_duration(path),
                "title": path.stem.replace("_", " ").replace("~", " ").strip(),
                "weight": weight,
            }
            if db.upsert_playable(c, row):
                added += 1
        c.commit()
    return added
