"""Time-of-day character: which kinds belong to which hours.

A pool that is merely shuffled feels like a playlist. A channel feels
programmed because the overnight hours look different from the evening
ones. This module supplies that as a selection-time factor, exactly the way
`seasons.py` supplies the calendar factor: computed from config on every
call, never written back, so the declared weight stays the operator's.

The same windows give the station's guide its programme blocks, which is
why each window carries a description meant for a viewer reading an EPG.
"""
import datetime
from pathlib import Path

import yaml

from bumparr import config

DAYPARTS_FILE = Path(__file__).resolve().parent / "config_files" / "dayparts.yaml"
MINUTES = 24 * 60


def _tz():
    """The configured zone, or None to mean the process's local zone."""
    if not config.TIMEZONE:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(config.TIMEZONE)
    except Exception as e:
        print("[dayparts] TZ %r unusable, using local time: %s" % (config.TIMEZONE, e))
        return None


def now_local(now=None):
    """An aware local datetime. Accepts None (now), naive (assumed local), or aware."""
    tz = _tz()
    if now is None:
        return datetime.datetime.now(tz) if tz else datetime.datetime.now().astimezone()
    if now.tzinfo is None:
        return now.replace(tzinfo=tz) if tz else now.astimezone()
    return now.astimezone(tz) if tz else now.astimezone()


def _minute(text):
    h, m = str(text).strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m != 0):
        raise ValueError("bad time %r" % text)
    return h * 60 + m


def _parse_hours(text):
    a, b = str(text).split("-")
    start, end = _minute(a), _minute(b)
    if start == end:
        raise ValueError("empty window %r" % text)
    return start, end


def _intervals(start, end):
    """Half-open minute intervals within one day; a wrapped window yields two."""
    if start < end:
        return [(start, end)]
    return [(start, MINUTES), (0, end)]


def load_dayparts(path=DAYPARTS_FILE):
    """{name: spec} from dayparts.yaml; {} on any problem.

    A broken or overlapping file degrades the station to "no dayparts"
    (every factor 1.0, brand-titled guide blocks) rather than guessing an
    order, and logs why, because a silent reorder of the day is the kind of
    wrong that nobody notices for weeks.
    """
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception as e:
        print("[dayparts] could not read %s: %s" % (path, e))
        return {}
    out = {}
    try:
        for name, spec in (doc.get("dayparts") or {}).items():
            start, end = _parse_hours(spec["hours"])
            kinds = {str(k): float(v) for k, v in (spec.get("kinds") or {}).items()}
            out[str(name)] = {
                "start": start,
                "end": end,
                "description": str(spec.get("description") or ""),
                "kinds": kinds,
            }
    except Exception as e:
        print("[dayparts] invalid %s: %s" % (path, e))
        return {}
    flat = sorted((iv, n) for n, s in out.items() for iv in _intervals(s["start"], s["end"]))
    for a, b in zip(flat, flat[1:]):
        if a[0][1] > b[0][0]:
            print("[dayparts] windows %r and %r overlap; ignoring %s" % (a[1], b[1], path))
            return {}
    return out


def _contains(spec, minute):
    return any(a <= minute < b for a, b in _intervals(spec["start"], spec["end"]))


def current(now=None, parts=None):
    """(name, spec) for the window containing `now`, or None."""
    parts = load_dayparts() if parts is None else parts
    t = now_local(now)
    minute = t.hour * 60 + t.minute
    for name, spec in parts.items():
        if _contains(spec, minute):
            return name, spec
    return None


def factors_now(now=None, parts=None):
    """{kind: multiplier} for right now; {} outside every window."""
    hit = current(now, parts)
    return dict(hit[1]["kinds"]) if hit else {}


def _next_start(parts, minute):
    starts = sorted(s["start"] for s in parts.values())
    for s in starts:
        if s > minute:
            return s
    return starts[0] if starts else None


def blocks(start, end, brand, parts=None):
    """Contiguous (start, end, title, description) blocks covering [start, end).

    Inside a window the block runs to the window's end (past midnight for a
    wrapped one). Outside every window the day is cut into brand-titled
    hours, ended early by the next window's start, so the guide never shows
    a programme that straddles a change of character.
    """
    parts = load_dayparts() if parts is None else parts
    out = []
    t, stop = now_local(start), now_local(end)
    while t < stop:
        minute = t.hour * 60 + t.minute
        midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
        hit = current(t, parts)
        if hit:
            name, spec = hit
            iv_end = next(
                b
                for a, b in _intervals(spec["start"], spec["end"])
                if a <= minute < b
            )
            block_end = midnight + datetime.timedelta(minutes=iv_end)
            if spec["start"] > spec["end"] and iv_end == MINUTES:
                block_end += datetime.timedelta(minutes=spec["end"])
            title, desc = "%s — %s" % (brand, name), spec["description"]
        else:
            block_end = t.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
            nxt = _next_start(parts, minute)
            if nxt is not None:
                candidate = midnight + datetime.timedelta(minutes=nxt)
                if candidate <= t:
                    candidate += datetime.timedelta(days=1)
                block_end = min(block_end, candidate)
            title, desc = brand, ""
        block_end = min(block_end, stop)
        if block_end <= t:
            break
        out.append((t, block_end, title, desc))
        t = block_end
    return out
