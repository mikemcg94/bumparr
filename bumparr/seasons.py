"""Seasonal weighting: keep holiday material out of rotation until it is wanted.

A turkey clip in April is jarring in a way no amount of good footage fixes, and
so is a Christmas tree in July. This computes a multiplier per category from the
calendar and applies it to the pool's weights.

Two design points worth stating.

It ramps rather than switching. Real channels tease a holiday for weeks and drop
it overnight afterwards, so weight rises through a lead-in, peaks on the day, and
falls off a short tail. A hard edge on one morning reads as a config change, not
as a season.

It lives in Bumparr, not the player. Deciding what may air is scheduling, but
deciding what is APPROPRIATE right now is a property of the material — and the
player is the licensed half. Keeping this on the production side means every
consumer gets seasonally-correct weights, not just the reference player.

The original weight is preserved in the payload the first time a category is
touched, so the calculation is idempotent and fully reversible.
"""
import argparse
import datetime
import json
from pathlib import Path

import yaml

from bumparr import config, db

SEASONS_FILE = Path(__file__).resolve().parent / "config_files" / "seasons.yaml"

# Weight multiplier while a category is in its window. Above 1.0 on purpose: a
# season should be noticeable, so October's pumpkins out-compete the everyday
# pool rather than merely joining it.
DEFAULT_IN_SEASON = 2.0
# Additional lift in the fortnight around the day itself.
DEFAULT_PEAK_BOOST = 1.5


def load_seasons(path=SEASONS_FILE):
    try:
        doc = yaml.safe_load(Path(path).read_text()) or {}
    except Exception as e:
        print("[seasons] could not read %s: %s" % (path, e))
        return {}
    return doc.get("seasons") or {}


def _doy(md, year):
    """MM-DD -> day-of-year for a given year, tolerating 02-29 on common years."""
    try:
        m, d = [int(x) for x in str(md).split("-")]
        return datetime.date(year, m, min(d, 28) if (m == 2 and d == 29) else d).timetuple().tm_yday
    except Exception:
        return None


def _circular_delta(a, b, year_len):
    """Shortest signed distance from a to b around a year-long circle."""
    d = (b - a) % year_len
    return d - year_len if d > year_len / 2 else d


def factor_for(spec, today=None):
    """Weight multiplier for a category, which may own SEVERAL dates.

    Fireworks are the case that forced this: they belong to New Year and to
    Independence Day equally, and a single window would have had to pick one.
    When a spec lists `windows`, each is evaluated and the strongest wins, so a
    category simply lights up near every date it belongs to.
    """
    windows = spec.get("windows")
    if windows:
        base = {k: v for k, v in spec.items() if k != "windows"}
        return max(_factor_one({**base, **w}, today) for w in windows)
    return _factor_one(spec, today)


def _factor_one(spec, today=None):
    """Weight multiplier for one season spec on a given date.

    Inside the window returns `in_season` (above 1.0, so the category outranks
    everyday material), lifted further near `peak`. Across the lead-in and tail
    it ramps between the off-season floor and that level. Outside entirely it
    returns `off_weight`, which is 0 for anything that should simply not air.
    """
    today = today or datetime.date.today()
    year_len = 366 if datetime.date(today.year, 12, 31).timetuple().tm_yday == 366 else 365
    now = today.timetuple().tm_yday

    start = _doy(spec.get("start"), today.year)
    end = _doy(spec.get("end"), today.year)
    if start is None or end is None:
        return 1.0
    off = float(spec.get("off_weight", 0.0))
    lead = int(spec.get("lead_in", 0))
    tail = int(spec.get("tail", 0))

    # Distance into the window, handling a window that wraps the year end.
    span = (end - start) % year_len
    into = (now - start) % year_len

    # In season, material should be PROMINENT, not merely permitted. October
    # ought to feel like October, which means Halloween clips beating the
    # everyday pool rather than tying with it.
    in_season = float(spec.get("in_season", DEFAULT_IN_SEASON))
    if into <= span:                       # inside the core window
        peak = _doy(spec.get("peak"), today.year)
        if peak is not None:
            # Approaching the day itself lifts weight further; the fall after is
            # handled by the tail rather than here.
            dist = abs(_circular_delta(now, peak, year_len))
            boost = float(spec.get("peak_boost", DEFAULT_PEAK_BOOST))
            return in_season + max(0.0, (1.0 - dist / 14.0)) * boost
        return in_season

    before = (start - now) % year_len      # days until the window opens
    after = (now - end) % year_len         # days since it closed

    # Ramps run between the off-season floor and the in-season level, so a
    # category eases up to prominence instead of appearing at full strength.
    if lead and before <= lead:
        return off + (in_season - off) * (1.0 - before / float(lead))
    if tail and after <= tail:
        return off + (in_season - off) * (1.0 - after / float(tail))
    return off


def factors_now(today=None):
    """{kind: multiplier} for right now, computed and never stored.

    This replaced writing weights into the database. Mutating a stored weight
    meant the seasonal pass, the editorial weight and the rotation model were
    all fighting over one column, which is why a `base_weight` shadow field had
    to exist to undo it. Handing back a factor lets the scoring model multiply
    it in at selection time and leaves the declared weight untouched.
    """
    today = today or datetime.date.today()
    return {kind: factor_for(spec, today) for kind, spec in load_seasons().items()}


def restore_base_weights():
    """Undo the historical in-place seasonal mutation.

    Earlier versions wrote the seasonally-adjusted value into `weight` and kept
    the original in `base_weight`. Those rows carry a weight that means nothing
    on its own, so put the declared value back and drop the shadow field.
    """
    restored = 0
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, weight, payload FROM playables WHERE payload LIKE '%base_weight%'").fetchall()]
        for r in rows:
            try:
                p = json.loads(r["payload"] or "{}")
            except Exception:
                continue
            base = p.pop("base_weight", None)
            if base is None:
                continue
            if abs(float(base) - float(r["weight"] or 0)) > 0.0005:
                c.execute("UPDATE playables SET weight=?, payload=? WHERE id=?",
                          (float(base), json.dumps(p), r["id"]))
                restored += 1
            else:
                c.execute("UPDATE playables SET payload=? WHERE id=?",
                          (json.dumps(p), r["id"]))
        c.commit()
    return restored


def apply(today=None, dry_run=False):
    """Recompute every gated category's weight for the current date."""
    seasons = load_seasons()
    if not seasons:
        return {"gated": 0, "changed": 0}

    today = today or datetime.date.today()
    changed, report = 0, []
    with db.conn() as c:
        for kind, spec in seasons.items():
            f = factor_for(spec, today)
            rows = [dict(r) for r in c.execute(
                "SELECT id, weight, payload FROM playables WHERE kind=?", (kind,)).fetchall()]
            if not rows:
                continue
            for r in rows:
                try:
                    p = json.loads(r["payload"] or "{}")
                except Exception:
                    p = {}
                # Remember the pre-seasonal weight once, so this is reversible
                # and repeated runs never compound.
                base = p.get("base_weight")
                if base is None:
                    base = r["weight"]
                    p["base_weight"] = base
                want = round(float(base) * f, 4)
                if abs(want - (r["weight"] or 0)) > 0.0005 or "base_weight" not in (
                        json.loads(r["payload"] or "{}")):
                    if not dry_run:
                        c.execute("UPDATE playables SET weight=?, payload=? WHERE id=?",
                                  (want, json.dumps(p), r["id"]))
                    changed += 1
            report.append((kind, len(rows), f, float(spec.get("off_weight", 0.0))))
        if not dry_run:
            c.commit()

    for kind, n, f, off in sorted(report, key=lambda x: -x[2]):
        # Compare against the category's OWN off-season level, not against 1.0.
        # Fireworks sit at a normal 1.0 year-round, and calling that "in season"
        # would misreport the one category that is never gated.
        if f <= off + 0.001:
            state = "baseline" if off >= 1.0 else ("out of season" if off < 0.05 else "reduced")
        elif f >= DEFAULT_IN_SEASON + 0.2:
            state = "PEAK"
        elif f >= DEFAULT_IN_SEASON - 0.001:
            state = "in season"
        else:
            state = "ramping"
        print("  %-16s %3d clip(s)  x%.2f  %s" % (kind, n, f, state))
    return {"gated": len(report), "changed": changed, "dry_run": dry_run}


def preview(kind, spec=None):
    """Show a category's weight curve across the year — useful for tuning."""
    seasons = load_seasons()
    spec = spec or seasons.get(kind)
    if not spec:
        print("no season defined for %r" % kind)
        return
    year = datetime.date.today().year
    out = []
    for m in range(1, 13):
        d = datetime.date(year, m, 15)
        out.append("%s %.2f" % (d.strftime("%b"), factor_for(spec, d)))
    print("  %-14s %s" % (kind, "  ".join(out)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Apply seasonal weighting to the pool.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--preview", action="store_true", help="show each curve across the year")
    ap.add_argument("--date", help="pretend it is this date (YYYY-MM-DD)")
    a = ap.parse_args()
    db.init_db()
    if a.preview:
        for k in load_seasons():
            preview(k)
    else:
        d = datetime.date.fromisoformat(a.date) if a.date else None
        print(apply(today=d, dry_run=a.dry_run))
