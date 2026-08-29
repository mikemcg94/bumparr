"""Run the shipped starter seeds to populate a fresh pool.

Bumparr ships no media. It ships a list of queries that are known to return
usable interstitial footage, so a new install has a first move instead of an
empty pool. Running them pulls from the user's own API keys into the user's own
library — the same path the ask-bar uses, just pre-typed.

Deliberately opt-in and never automatic on startup: downloading gigabytes
because someone started a container is not a decision software should make for
its user, and the archives throttle bursts.
"""
import argparse
import time
from pathlib import Path

import yaml

from bumparr import config, db, ingest

SEEDS_FILE = Path(__file__).resolve().parent / "config_files" / "starter_seeds.yaml"

# Space out archive.org requests. A parallel burst gets the household WAN
# rate-limited for hours, which costs far more than the wait.
ARCHIVE_DELAY = 9.0
STOCK_DELAY = 1.5


def load_seeds(path=SEEDS_FILE):
    try:
        doc = yaml.safe_load(Path(path).read_text()) or {}
    except Exception as e:
        print("[starter] could not read %s: %s" % (path, e))
        return []
    return doc.get("seeds") or []


def _have_keys():
    return bool(config.PEXELS_API_KEY or config.PIXABAY_API_KEY)


def run(limit=None, dry_run=False, only_free=False):
    seeds = load_seeds()
    if limit:
        seeds = seeds[:limit]
    if not seeds:
        print("[starter] no seeds found")
        return []

    stock_ok = _have_keys()
    if not stock_ok and not only_free:
        print("[starter] NOTE: no PEXELS_API_KEY or PIXABAY_API_KEY set, so stock "
              "queries will find nothing. Both are free and self-signup; set one "
              "and re-run, or use --only-free for the archive entries.")

    results = []
    for i, s in enumerate(seeds):
        query = s.get("query")
        if not query:
            continue
        is_archive = s.get("source") == "archive"
        if only_free and not is_archive:
            continue
        if is_archive is False and not stock_ok:
            continue
        count = int(s.get("count") or 3)
        label = "archive" if is_archive else "stock"

        if dry_run:
            print("  would pull %2d  %-8s %-42s  %s"
                  % (count, label, query, s.get("why", "")))
            continue

        print("  [%d/%d] %-8s %-40s" % (i + 1, len(seeds), label, query[:40]), flush=True)
        try:
            # The ask-bar's own handler: one path for typed and seeded requests,
            # so a seed can never behave differently from typing the same words.
            msg = ingest.handle("%d %s" % (count, query))
        except Exception as e:
            msg = "failed: %s" % e
        print("       %s" % str(msg)[:150])
        results.append((query, msg))
        time.sleep(ARCHIVE_DELAY if is_archive else STOCK_DELAY)

    if not dry_run:
        print("[starter] ran %d seed(s)" % len(results))
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Populate a fresh pool from the shipped starter seeds.")
    ap.add_argument("--dry-run", action="store_true", help="list what would be pulled")
    ap.add_argument("--limit", type=int, help="only the first N seeds")
    ap.add_argument("--only-free", action="store_true",
                    help="skip stock-API entries; archive.org needs no key")
    a = ap.parse_args()
    db.init_db()
    run(limit=a.limit, dry_run=a.dry_run, only_free=a.only_free)
