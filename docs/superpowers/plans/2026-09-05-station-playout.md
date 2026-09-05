# Station Playout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the bumper pool as two always-on HLS channels (`live` and `standby`) with an XMLTV guide, so Dispatcharr can carry Bumparr as a first-class channel and use it as branded failover, while the playout becomes the first real writer of play history.

**Architecture:** A background conform job transcodes every eligible playable once into a fixed profile, pre-cut into MPEG-TS segments under `ASSET_ROOT/.cache/station/<key>/`. A virtual-clock playout per channel picks items through `rotation.weights_for` (now with a daypart factor), extends its timeline lazily on every playlist request, and writes `play_history` when an item's start time passes. Serving is a dict lookup plus a static file. Dayparts add time-of-day weighting and give the guide honest block titles.

**Tech Stack:** Python 3.12, FastAPI, SQLite via `bumparr.db`, `unittest` (not pytest), ffmpeg (runtime only; never in CI), `ruff` (F401/F821 via `ruff.toml`), Node built-in test runner for the dashboard.

**Spec:** `docs/superpowers/specs/2026-09-05-station-playout-design.md`. Read it first. This plan argues from it and does not repeat its rationale.

## Global Constraints

- **Test runner is `unittest`.** `python -m unittest discover -s tests`. Every test file is a `unittest.TestCase`. Local 3.12 interpreter: `python3.12 -m venv .venv` (see CONTRIBUTING).
- **CI has no ffmpeg/ffprobe.** Every test that reaches `subprocess.Popen`/`subprocess.run` MUST mock it. Conform tests assert the argv and fake the segment list ffmpeg would have written.
- **`config.DB_PATH`, `config.ASSET_ROOT`, `config.OUTPUT_DIR` are read at call time.** Tests patch the `config` module attributes in `setUp` using the exact idiom in `tests/test_seed.py:10-20`. New code MUST read `config.X` at call time too, never cache it at import (the segment `StaticFiles` mount is the one accepted exception, matching the existing media mounts).
- **Nothing heavy in the request path.** A playlist request may read `index.json` files and the registry, and write play history. It may never launch ffmpeg.
- **Live-cam `stream` rows never enter the station.** Only `video`, `card`, `image`.
- **Declared `weight` is never written by the station.** The playout writes only `last_played`, `play_count`, `play_history`, `playout`.
- **Docstrings explain why.** Module and public-function docstrings are load-bearing (CONTRIBUTING). Match the surrounding density.
- **Docs and code change together.** Endpoints → `docs/API.md`; settings → `docs/CONFIG.md` + `.env.example`; rotation → `docs/ROTATION.md`.
- **Conventional commits**, one per task: `feat:`, `refactor:`, `docs:`, `test:`.
- **Full gate** (run before every commit):

```bash
python -m compileall -q bumparr
python -W error::ResourceWarning -m unittest discover -s tests
ruff check bumparr tests
node --check bumparr/web/app.js && node --test bumparr/web/app.test.js
```

## File Structure

| File | Task | Responsibility |
|---|---|---|
| `bumparr/dayparts.py` | 1 | **Create.** Parse `dayparts.yaml`; current window; `factors_now`; guide `blocks` |
| `bumparr/config_files/dayparts.yaml` | 1 | **Create.** Shipped default dayparts |
| `tests/test_dayparts.py` | 1 | **Create** |
| `bumparr/rotation.py` | 2 | Add the `daypart` factor to `score`/`explain`/`build_context`/`weights_for` |
| `bumparr/app.py` (`random_bumpers`) | 2 | Pass `dayparts.factors_now()` |
| `docs/ROTATION.md`, `tests/test_rotation.py` | 2 | Document and test the factor |
| `bumparr/config.py` | 3 | Six `STATION_*`/`STANDBY_KINDS` settings |
| `bumparr/urls.py` | 3 | **Create.** `public_base`, `absolutize` moved out of `app.py` |
| `docs/CONFIG.md`, `.env.example` | 3 | Document the settings |
| `bumparr/station/__init__.py` | 4 | **Create.** Package docstring only |
| `bumparr/station/conform.py` | 4, 5 | **Create.** Cache key, ffmpeg argv, conform one, sweep, prune, slate |
| `bumparr/jobs.py` | 4 | `station_conform_loop()` |
| `bumparr/app.py` (lifespan) | 4 | Start the loop |
| `tests/test_station_conform.py` | 4, 5 | **Create** |
| `bumparr/station/playout.py` | 6 | **Create.** `Entry`, `Channel`, registry of channels |
| `tests/test_station_playout.py` | 6 | **Create** |
| `bumparr/station/guide.py` | 7 | **Create.** XMLTV |
| `tests/test_station_guide.py` | 7 | **Create** |
| `bumparr/station/routes.py` | 8 | **Create.** `/station/*` and `GET /api/station` |
| `bumparr/app.py` (router, mount, `POST /api/station/conform`) | 8 | Wire the router and the conform action |
| `requirements-dev.txt` | 8 | `httpx` for FastAPI's TestClient |
| `tests/test_station_routes.py`, `docs/API.md` | 8 | Route tests and reference |
| `bumparr/web/index.html`, `app.js`, `style.css`, `app.test.js` | 9 | Station panel |
| `docs/INTEGRATION.md`, `README.md`, `docs/ARCHITECTURE.md`, `CHANGELOG.md` | 10 | Integration story |
| this plan, "Acceptance" section | 11 | Written result of the Dispatcharr check |

Tasks 1–3 are independent of each other. Task 4 needs 3. Task 5 needs 4. Task 6 needs 2, 4. Task 7 needs 1. Task 8 needs 3, 6, 7. Task 9 needs 8. Task 10 needs 8. Task 11 needs everything.

---

### Task 1: Dayparts

**Files:**
- Create: `bumparr/dayparts.py`
- Create: `bumparr/config_files/dayparts.yaml`
- Test: `tests/test_dayparts.py`

**Interfaces:**
- Produces:
  - `load_dayparts(path=DAYPARTS_FILE) -> dict[str, dict]` — `{name: {"start": int_minute, "end": int_minute, "description": str, "kinds": {kind: float}}}`; `{}` on missing file, parse error, or overlapping windows.
  - `now_local(now=None) -> datetime` — aware local datetime (uses `config.TIMEZONE` when set). Accepts naive-local, aware, or None.
  - `current(now=None, parts=None) -> (name, spec) | None`
  - `factors_now(now=None, parts=None) -> dict[str, float]`
  - `blocks(start, end, brand, parts=None) -> list[(start_dt, end_dt, title, description)]` contiguous over `[start, end)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dayparts.py
import datetime
import tempfile
import unittest
from pathlib import Path

from bumparr import dayparts

PARTS = """
dayparts:
  overnight:
    hours: "22:00-06:00"
    description: "Windows and dead air."
    kinds: {window: 2.0, trivia: 0.4}
  morning:
    hours: "06:00-10:00"
    description: "Weather and the time."
    kinds: {weather: 3.0}
  evening:
    hours: "18:00-22:00"
    kinds: {trivia: 2.0}
"""

TZ = datetime.timezone(datetime.timedelta(hours=-4))


def at(h, m=0, day=5):
    return datetime.datetime(2026, 9, day, h, m, tzinfo=TZ)


class Parsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "dayparts.yaml"

    def write(self, text):
        self.path.write_text(text, encoding="utf-8"); return self.path

    def test_windows_parse_including_wraparound(self):
        parts = dayparts.load_dayparts(self.write(PARTS))
        self.assertEqual(parts["overnight"]["start"], 22 * 60)
        self.assertEqual(parts["overnight"]["end"], 6 * 60)
        self.assertEqual(parts["morning"]["kinds"], {"weather": 3.0})
        self.assertEqual(parts["evening"]["description"], "")

    def test_missing_or_broken_file_means_no_dayparts(self):
        self.assertEqual(dayparts.load_dayparts(self.path / "nope.yaml"), {})
        self.assertEqual(dayparts.load_dayparts(self.write("dayparts: {a: {hours: 'x'}}")), {})

    def test_overlapping_windows_disable_the_file(self):
        text = PARTS + "  clash:\n    hours: \"09:00-11:00\"\n"
        self.assertEqual(dayparts.load_dayparts(self.write(text)), {})


class Selection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        p = Path(self.tmp.name) / "d.yaml"; p.write_text(PARTS, encoding="utf-8")
        self.parts = dayparts.load_dayparts(p)

    def test_boundaries_are_half_open(self):
        self.assertEqual(dayparts.current(at(5, 59), self.parts)[0], "overnight")
        self.assertEqual(dayparts.current(at(6, 0), self.parts)[0], "morning")
        self.assertEqual(dayparts.current(at(23, 30), self.parts)[0], "overnight")
        self.assertEqual(dayparts.current(at(1, 0), self.parts)[0], "overnight")
        self.assertIsNone(dayparts.current(at(12, 0), self.parts))

    def test_factors_follow_the_current_window(self):
        self.assertEqual(dayparts.factors_now(at(7), self.parts), {"weather": 3.0})
        self.assertEqual(dayparts.factors_now(at(12), self.parts), {})
        self.assertEqual(dayparts.factors_now(at(3), self.parts), {"window": 2.0, "trivia": 0.4})

    def test_blocks_are_contiguous_and_fill_gaps_with_brand_hours(self):
        out = dayparts.blocks(at(9, 30), at(19, 0), "TV", self.parts)
        self.assertEqual(out[0][0], at(9, 30))
        self.assertEqual(out[-1][1], at(19, 0))
        for a, b in zip(out, out[1:]):
            self.assertEqual(a[1], b[0])
        self.assertEqual(out[0][2], "TV — morning")
        self.assertEqual(out[1], (at(10), at(11), "TV", ""))
        self.assertEqual(out[-1][2], "TV — evening")
        self.assertEqual(out[-1][0], at(18))

    def test_wrapped_window_block_runs_past_midnight(self):
        out = dayparts.blocks(at(21), at(7, day=6), "TV", self.parts)
        titles = [(s, e, t) for s, e, t, _ in out]
        self.assertIn((at(22), at(6, day=6), "TV — overnight"), titles)
        self.assertEqual(out[-1][2], "TV — morning")

    def test_no_parts_means_brand_hours_only(self):
        out = dayparts.blocks(at(9, 30), at(12), "TV", {})
        self.assertEqual([t for _, _, t, _ in out], ["TV", "TV", "TV"])
        self.assertEqual(out[0][1], at(10))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to confirm failure**

Run: `python -m unittest tests.test_dayparts -v`
Expected: every test errors with `ModuleNotFoundError: No module named 'bumparr.dayparts'`.

- [ ] **Step 3: Create `bumparr/config_files/dayparts.yaml`**

```yaml
# Dayparts — the station's time-of-day character.
#
# A real channel is programmed, not shuffled: overnight is windows and dead
# air, mornings are the weather and the time, evenings are trivia. A window
# here names a block of the local day (TZ) and says which kinds belong to it.
#
#   hours        "HH:MM-HH:MM", 24-hour, local time. A window may wrap
#                midnight ("22:00-06:00"). Half-open: 06:00 belongs to the
#                window that STARTS at 06:00.
#   kinds        {kind: multiplier}. Stacks with season and the play-history
#                factors at selection time; a kind not listed is 1.0. Nothing
#                is written to the database.
#   description  becomes the guide programme text for the block, so write it
#                for a viewer reading an EPG, not for yourself.
#
# Windows must not overlap; if they do the whole file is ignored (logged) so
# a typo cannot silently reorder your day. Hours with no window play the
# full rotation and appear in the guide as a plain brand-titled block.
#
# THESE ARE SUGGESTIONS. Edit freely: your hours, your channel.
dayparts:
  overnight:
    hours: "00:00-06:00"
    description: "Windows, dead air, and the occasional unexplained number."
    kinds: {window: 2.0, dead_air: 2.0, number: 1.5, trivia: 0.4, psa: 0.5}
  morning:
    hours: "06:00-10:00"
    description: "Weather, the time, and the day's on-this-day."
    kinds: {weather: 3.0, local_time: 2.5, on_this_day: 2.0, dead_air: 0.2}
  daytime:
    hours: "10:00-18:00"
    description: "The full rotation."
  evening:
    hours: "18:00-24:00"
    description: "Trivia, tiny games, and the odd correction."
    kinds: {trivia: 2.0, tiny_games: 2.0, corrections: 1.5, dead_air: 0.3}
```

- [ ] **Step 4: Create `bumparr/dayparts.py`**

```python
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
    order, and says so on stdout, because a silent reorder of the day is the
    kind of wrong that nobody notices for weeks.
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
            out[str(name)] = {"start": start, "end": end,
                              "description": str(spec.get("description") or ""),
                              "kinds": kinds}
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
            iv_end = next(b for a, b in _intervals(spec["start"], spec["end"]) if a <= minute < b)
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
```

`"00:00-06:00"` parses to `(0, 360)`, `"18:00-24:00"` to `(1080, 1440)`, and `"22:00-06:00"` to `(1320, 360)`, which `_intervals` splits into two half-open ranges.

- [ ] **Step 5: Run the tests**

Run: `python -m unittest tests.test_dayparts -v`
Expected: 9 tests, OK. If `test_wrapped_window_block_runs_past_midnight` fails on the `at(6, day=6)` end, check `blocks()`'s wrapped-window branch: for `minute` in `(1320, 1440)` the block end is next midnight plus `spec["end"]` minutes.

- [ ] **Step 6: Full gate, then commit**

```bash
git add bumparr/dayparts.py bumparr/config_files/dayparts.yaml tests/test_dayparts.py
git commit -m "feat: dayparts — time-of-day windows as a selection factor and guide blocks"
```

---

### Task 2: Daypart factor in the rotation model

**Files:**
- Modify: `bumparr/rotation.py:98-157` (`score`, `explain`, `build_context`, `weights_for`, module docstring line 21)
- Modify: `bumparr/app.py` `random_bumpers` (the block that calls `weights_for`)
- Modify: `docs/ROTATION.md` (formula and factor table)
- Test: `tests/test_rotation.py` (append)

**Interfaces:**
- Produces: `rotation.build_context(rows, season_factors=None, now=None, daypart_factors=None)` and `rotation.weights_for(rows, season_factors=None, now=None, daypart_factors=None)`; `ctx["daypart"]` is `{kind: factor}`; `explain()` gains a `"daypart"` key. Existing positional callers are unaffected.

- [ ] **Step 1: Append failing tests to `tests/test_rotation.py`**

```python
class DaypartFactor(unittest.TestCase):
    def test_daypart_multiplies_and_defaults_to_one(self):
        from bumparr import rotation
        rows = [{"id": "a", "kind": "trivia", "weight": 1.0, "last_played": 0, "play_count": 0},
                {"id": "b", "kind": "window", "weight": 1.0, "last_played": 0, "play_count": 0}]
        plain, _ = rotation.weights_for(rows, None, 1000.0)
        boosted, ctx = rotation.weights_for(rows, None, 1000.0, {"trivia": 2.0})
        self.assertAlmostEqual(boosted[0], plain[0] * 2.0)
        self.assertAlmostEqual(boosted[1], plain[1])
        self.assertEqual(ctx["daypart"], {"trivia": 2.0})
        self.assertEqual(rotation.explain(rows[0], ctx, 1000.0)["daypart"], 2.0)
        self.assertEqual(rotation.explain(rows[1], ctx, 1000.0)["daypart"], 1.0)
```

Run: `python -m unittest tests.test_rotation.DaypartFactor -v`
Expected: FAIL (`TypeError: weights_for() takes from 1 to 3 positional arguments`).

- [ ] **Step 2: Implement**

In `bumparr/rotation.py`:

Module docstring line 21: `score = base x season x daypart x recency x affinity x fatigue`, and add a line under the factor list: `daypart     does this kind belong to this hour of the day (dayparts.yaml)`.

`score`: after the `s = ...` line add
```python
    d = float((ctx.get("daypart") or {}).get(item.get("kind"), 1.0))
```
and return `base * s * d * r * a * f`.

`explain`: same `d` line; add `"daypart": round(d, 3)` to the dict and multiply `d` into `"score"`.

`build_context(rows, season_factors=None, now=None, daypart_factors=None)`: add `"daypart": daypart_factors or {}` to the returned dict.

`weights_for(rows, season_factors=None, now=None, daypart_factors=None)`: pass `daypart_factors` through to `build_context`.

In `bumparr/app.py` `random_bumpers`, replace

```python
    from bumparr import rotation, seasons
    try:
        season = seasons.factors_now()
    except Exception:
        season = {}
    weights, _ = rotation.weights_for(pool, season)
```
with
```python
    from bumparr import dayparts, rotation, seasons
    try:
        season = seasons.factors_now()
    except Exception:
        season = {}
    try:
        daypart = dayparts.factors_now()
    except Exception:
        daypart = {}
    weights, _ = rotation.weights_for(pool, season, None, daypart)
```

- [ ] **Step 3: Update `docs/ROTATION.md`**

Formula line becomes `score = base × season × daypart × recency × affinity × fatigue`. Add a table row after `season`:

```
| `daypart` | `dayparts.py`, from `config_files/dayparts.yaml` | does this kind belong to this hour of the local day |
```

And a short section after "Seasonality":

```markdown
## Dayparts

`dayparts.py` supplies the `daypart` factor per kind from
`config_files/dayparts.yaml`: named windows of the local day, each with its
own kind multipliers. Like the season factor it is computed at selection time
and never stored. Outside every window the factor is 1.0 for everything. The
same windows title the station's guide blocks (see the station docs), which
is why each carries a viewer-facing description.
```

- [ ] **Step 4: Full gate, then commit**

```bash
git add bumparr/rotation.py bumparr/app.py docs/ROTATION.md tests/test_rotation.py
git commit -m "feat: daypart factor in the rotation model"
```

---

### Task 3: Station settings and the URL helper module

**Files:**
- Modify: `bumparr/config.py` (append after `PLAYABLE_TYPES`)
- Create: `bumparr/urls.py`
- Modify: `bumparr/app.py` (`_LOOPBACK_WARNED`, `_public_base`, `_absolutize`)
- Modify: `docs/CONFIG.md`, `.env.example`

**Interfaces:**
- Produces: `config.STATION_SEGMENT_SECONDS`, `config.STATION_WINDOW_SEGMENTS`, `config.STATION_CONFORM_INTERVAL`, `config.STATION_CONFORM_TIMEOUT`, `config.STATION_BITRATE_K` (ints), `config.STANDBY_KINDS` (tuple of str); `urls.public_base(request) -> str`, `urls.absolutize(url, request) -> str`.

- [ ] **Step 1: Settings**

Append to `bumparr/config.py`:

```python
# The station: the pool run as a live channel (see docs/superpowers/specs/
# 2026-09-05-station-playout-design.md). Segment length is also the keyframe
# cadence divisor (GOP is 2s, so 4s segments always start on a keyframe).
STATION_SEGMENT_SECONDS = int(env("STATION_SEGMENT_SECONDS", "4"))
STATION_WINDOW_SEGMENTS = int(env("STATION_WINDOW_SEGMENTS", "6"))
STATION_CONFORM_INTERVAL = int(env("STATION_CONFORM_INTERVAL", "300"))   # 0 disables
STATION_CONFORM_TIMEOUT = int(env("STATION_CONFORM_TIMEOUT", "600"))
STATION_BITRATE_K = int(env("STATION_BITRATE_K", "4000"))
# What the standby channel may air: the material that reads as "we know, hold
# on" rather than programming. Window captures are in because a live view of
# somewhere is the classic hold pattern.
STANDBY_KINDS = tuple(k.strip() for k in env(
    "STANDBY_KINDS", "technical_difficulties,station_id,dead_air,window").split(",") if k.strip())
```

- [ ] **Step 2: Create `bumparr/urls.py`** by moving code out of `app.py`

Cut `_LOOPBACK_WARNED = False`, `def _public_base(request)`, and `def _absolutize(url, request)` from `bumparr/app.py` **verbatim** (keep their docstrings and the loopback warning) into a new file with this header, renaming them `public_base` and `absolutize` and the flag `_LOOPBACK_WARNED`:

```python
"""Absolute URLs for everything Bumparr hands out.

Lives outside app.py because the station router needs the same rule and
must not import the app to get it. The rule itself: explicit PUBLIC_URL
wins; otherwise mirror the request, and warn once when that mirror is a
loopback address, because that playlist looks fine and plays nowhere.
"""
import re

from bumparr import config

_LOOPBACK_WARNED = False
```

Then in `app.py` add `from bumparr.urls import absolutize as _absolutize, public_base as _public_base` to the imports and remove the moved code. Every existing call site keeps its name. Run `ruff check bumparr` to catch a now-unused `re` import in `app.py` (remove it only if nothing else uses it).

- [ ] **Step 3: Document**

`docs/CONFIG.md`: add a `## Station` table:

```markdown
| Variable | Default | Meaning |
|---|---|---|
| `STATION_SEGMENT_SECONDS` | `4` | HLS segment length for the live channels. |
| `STATION_WINDOW_SEGMENTS` | `6` | Segments in the live window; lookahead = window × segment. |
| `STATION_CONFORM_INTERVAL` | `300` | Seconds between conform sweeps. `0` disables the loop (the API action still works). |
| `STATION_CONFORM_TIMEOUT` | `600` | Per-item ffmpeg ceiling when conforming. |
| `STATION_BITRATE_K` | `4000` | Target video bitrate (kbit/s) of the conformed profile. |
| `STANDBY_KINDS` | `technical_difficulties,station_id,dead_air,window` | Kinds the standby channel may air. |
```

`.env.example`: under a new `# --- Station ---` block, the same six as commented lines with one-line hints.

- [ ] **Step 4: Full gate, then commit**

```bash
git add bumparr/config.py bumparr/urls.py bumparr/app.py docs/CONFIG.md .env.example
git commit -m "feat: station settings and a shared URL helper module"
```

---

### Task 4: Conform

**Files:**
- Create: `bumparr/station/__init__.py`
- Create: `bumparr/station/conform.py`
- Modify: `bumparr/jobs.py` (append `station_conform_loop`)
- Modify: `bumparr/app.py` lifespan `tasks` list
- Test: `tests/test_station_conform.py`

**Interfaces:**
- Consumes: `config.STATION_*`, `paths.resolve_media(uri)`, `db.conn()`.
- Produces:
  - `cache_dir() -> Path` (`ASSET_ROOT/.cache/station`, read at call time)
  - `cache_key(row_id, mtime_ns, size) -> str` (24 hex chars)
  - `ffmpeg_path() -> str | None`, `has_audio(src) -> bool`
  - `ffmpeg_command(src, out_dir, *, audio, still=False, duration=None) -> list[str]`
  - `conform_source(row_id, src, *, still=False, duration=None) -> key` (raises `RuntimeError` on failure; never leaves `<key>.part`)
  - `eligible_rows(conn) -> list[dict]`, `expected_key(row) -> (key | None, Path | None)`
  - `load_index() -> {id: index_dict}` (newest `conformed_at` wins per id)
  - `sweep(limit=None, keep=frozenset()) -> dict` with `conformed, failed, pruned, skipped, ffmpeg, busy?`
  - `SLATE_KEY = "slate"`; `ensure_slate()` is Task 5 (sweep calls it inside a try/except; stub it as `def ensure_slate(): return None` in this task)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_station_conform.py
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bumparr import config, db
from bumparr.station import conform


class _Proc:
    """A fake ffmpeg: writes the segment list the real one would, then exits."""
    def __init__(self, out_dir, segs=(4.0, 4.0, 2.07), returncode=0, hang=False):
        self.out_dir, self.segs, self.returncode, self.hang = Path(out_dir), segs, returncode, hang
        self.killed = False

    def communicate(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        if self.returncode == 0 and not self.killed:
            lines = ["#EXTM3U"]
            for i, d in enumerate(self.segs):
                (self.out_dir / ("%03d.ts" % i)).write_bytes(b"ts")
                lines += ["#EXTINF:%.6f," % d, "%03d.ts" % i]
            (self.out_dir / "segments.m3u8").write_text("\n".join(lines), encoding="utf-8")
        return b"", b"diag tail"

    def kill(self):
        self.killed = True; self.returncode = -9


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.originals = config.DB_PATH, config.ASSET_ROOT, config.OUTPUT_DIR
        config.DB_PATH = str(Path(self.tmp.name) / "s.db")
        config.ASSET_ROOT = Path(self.tmp.name) / "assets"
        config.OUTPUT_DIR = config.ASSET_ROOT / "bumpers"
        config.ASSET_ROOT.mkdir(); config.OUTPUT_DIR.mkdir()
        for attr, value in zip(("DB_PATH", "ASSET_ROOT", "OUTPUT_DIR"), self.originals):
            self.addCleanup(setattr, config, attr, value)
        db.init_db()
        self.src = config.OUTPUT_DIR / "clip.mp4"; self.src.write_bytes(b"x" * 100)

    def popen(self, **kw):
        def fake(cmd, **_):
            out_dir = Path(cmd[-1]).parent
            return _Proc(out_dir, **kw)
        return mock.patch.object(subprocess, "Popen", side_effect=fake)


class Command(Base):
    def test_audio_source_maps_its_own_track_and_pads(self):
        cmd = conform.ffmpeg_command(self.src, Path("/o"), audio=True)
        self.assertIn("0:a:0", cmd); self.assertNotIn("anullsrc=r=48000:cl=stereo", cmd)
        self.assertIn("aresample=async=1,apad", cmd)
        self.assertEqual(cmd[-1], "/o/%03d.ts")
        self.assertIn("-segment_time", cmd); self.assertIn(str(config.STATION_SEGMENT_SECONDS), cmd)

    def test_silent_source_gets_a_synthesized_track(self):
        cmd = conform.ffmpeg_command(self.src, Path("/o"), audio=False)
        self.assertIn("anullsrc=r=48000:cl=stereo", cmd); self.assertIn("1:a:0", cmd)

    def test_still_loops_for_its_duration(self):
        cmd = conform.ffmpeg_command(self.src, Path("/o"), audio=False, still=True, duration=7.5)
        self.assertEqual(cmd[cmd.index("-loop") + 1], "1")
        self.assertEqual(cmd[cmd.index("-t") + 1], "7.500")

    def test_profile_is_fixed(self):
        cmd = conform.ffmpeg_command(self.src, Path("/o"), audio=True)
        for flag, value in (("-g", "60"), ("-sc_threshold", "0"), ("-profile:v", "high"),
                            ("-level", "4.1"), ("-ar", "48000"), ("-ac", "2")):
            self.assertEqual(cmd[cmd.index(flag) + 1], value)
        self.assertIn("scale=1920:1080:force_original_aspect_ratio=decrease", " ".join(cmd))


class ConformOne(Base):
    def test_lands_atomically_with_index(self):
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=True):
            key = conform.conform_source("vid:clip", self.src)
        d = conform.cache_dir() / key
        idx = json.loads((d / "index.json").read_text())
        self.assertEqual(idx["id"], "vid:clip"); self.assertEqual(idx["key"], key)
        self.assertEqual(idx["segments"], [4.0, 4.0, 2.07]); self.assertAlmostEqual(idx["duration"], 10.07)
        self.assertTrue((d / "002.ts").exists()); self.assertFalse((d / "segments.m3u8").exists())
        self.assertFalse((conform.cache_dir() / (key + ".part")).exists())

    def test_is_idempotent_for_an_unchanged_source(self):
        with self.popen() as p, mock.patch.object(conform, "has_audio", return_value=True):
            k1 = conform.conform_source("vid:clip", self.src)
            k2 = conform.conform_source("vid:clip", self.src)
        self.assertEqual(k1, k2); self.assertEqual(p.call_count, 1)

    def test_nonzero_exit_leaves_nothing_and_reports_tail(self):
        with self.popen(returncode=1), mock.patch.object(conform, "has_audio", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "diag tail"):
                conform.conform_source("vid:clip", self.src)
        self.assertEqual(list(conform.cache_dir().iterdir()), [])

    def test_timeout_kills_and_cleans(self):
        with self.popen(hang=True), mock.patch.object(conform, "has_audio", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                conform.conform_source("vid:clip", self.src)
        self.assertEqual(list(conform.cache_dir().iterdir()), [])

    def test_key_changes_with_the_source(self):
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=False):
            k1 = conform.conform_source("vid:clip", self.src)
            self.src.write_bytes(b"y" * 200)
            k2 = conform.conform_source("vid:clip", self.src)
        self.assertNotEqual(k1, k2)


class Sweep(Base):
    def seed(self, rows):
        with db.conn() as c:
            for r in rows:
                c.execute("INSERT INTO playables (id,type,kind,uri,duration,enabled,health) VALUES (?,?,?,?,?,?,?)", r)
            c.commit()

    def test_conforms_eligible_prunes_stale_and_skips_streams(self):
        (config.ASSET_ROOT / "still.png").write_bytes(b"png")
        self.seed([("vid:clip", "video", "ambient", "bumpers/clip.mp4", 10, 1, "ok"),
                   ("img:still.png", "image", "art", "still.png", 8, 1, "ok"),
                   ("stream:cam", "stream", "webcam", "https://x/y.m3u8", 0, 1, "ok"),
                   ("vid:off", "video", "ambient", "bumpers/clip.mp4", 10, 0, "ok")])
        stale = conform.cache_dir() / "deadbeefdeadbeefdeadbeef"; stale.mkdir(parents=True)
        (stale / "index.json").write_text(json.dumps({"id": "gone", "key": stale.name, "segments": [1], "duration": 1, "conformed_at": 1}))
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=False), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"):
            stats = conform.sweep()
        self.assertEqual((stats["conformed"], stats["failed"], stats["pruned"]), (2, 0, 1))
        self.assertFalse(stale.exists())
        self.assertEqual(set(conform.load_index()), {"vid:clip", "img:still.png"})

    def test_one_failure_does_not_stop_the_sweep(self):
        (config.OUTPUT_DIR / "bad.mp4").write_bytes(b"bad")
        self.seed([("vid:bad", "video", "a", "bumpers/bad.mp4", 5, 1, "ok"),
                   ("vid:clip", "video", "a", "bumpers/clip.mp4", 5, 1, "ok")])
        calls = {"n": 0}
        def fake(cmd, **_):
            calls["n"] += 1
            return _Proc(Path(cmd[-1]).parent, returncode=1 if "bad" in " ".join(cmd) else 0)
        with mock.patch.object(subprocess, "Popen", side_effect=fake), \
                mock.patch.object(conform, "has_audio", return_value=False), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"):
            stats = conform.sweep()
        self.assertEqual((stats["conformed"], stats["failed"]), (1, 1))

    def test_keep_protects_keys_on_air(self):
        live = conform.cache_dir() / "aaaaaaaaaaaaaaaaaaaaaaaa"; live.mkdir(parents=True)
        (live / "index.json").write_text(json.dumps({"id": "gone", "key": live.name, "segments": [1], "duration": 1, "conformed_at": 1}))
        with mock.patch.object(conform, "ffmpeg_path", return_value=None):
            stats = conform.sweep(keep={live.name})
        self.assertEqual(stats["pruned"], 0); self.assertTrue(live.exists()); self.assertFalse(stats["ffmpeg"])

    def test_limit_bounds_a_pass(self):
        (config.OUTPUT_DIR / "b.mp4").write_bytes(b"b")
        self.seed([("vid:a", "video", "a", "bumpers/clip.mp4", 5, 1, "ok"),
                   ("vid:b", "video", "a", "bumpers/b.mp4", 5, 1, "ok")])
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=False), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"):
            self.assertEqual(conform.sweep(limit=1)["conformed"], 1)


if __name__ == "__main__":
    unittest.main()
```

Run: `python -m unittest tests.test_station_conform -v`
Expected: import error for `bumparr.station`.

- [ ] **Step 2: Create the package**

`bumparr/station/__init__.py`:

```python
"""The station: the bumper pool run as live HLS channels.

conform  — one-time transcode of each playable into splice-safe segments
playout  — a virtual clock per channel that picks, publishes, and reports
guide    — XMLTV for the channels
routes   — the HTTP surface

Design: docs/superpowers/specs/2026-09-05-station-playout-design.md.
"""
```

- [ ] **Step 3: Create `bumparr/station/conform.py`**

```python
"""Make every playable splice-safe, once, off the request path.

Bumparr's outputs are all 1080p30 H.264, but "all H.264" is not "spliceable":
GOP lengths differ, some have no audio track, produced clips carry 128k
audio and cards 96k, and a few sources are not 1080p at all. A live HLS
channel built from them has to stitch segments from different files
back-to-back, and a player will only do that cleanly when every segment
shares one profile and starts on a keyframe. So each item is transcoded once
into that profile and pre-cut into MPEG-TS segments, keyed by the source
file's identity so a re-rendered card is re-conformed and nothing is ever
conformed twice. Serving the channel then touches no encoder at all.
"""
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from bumparr import config, db, paths

SLATE_KEY = "slate"
_LOCK = threading.Lock()


def cache_dir():
    """Where conformed segments live; under .cache so seed.py never registers them."""
    return Path(config.ASSET_ROOT) / ".cache" / "station"


def cache_key(row_id, mtime_ns, size):
    return hashlib.sha256(("%s\0%d\0%d" % (row_id, mtime_ns, size)).encode("utf-8")).hexdigest()[:24]


def ffmpeg_path():
    return shutil.which("ffmpeg")


def has_audio(src):
    """Whether the source carries an audio stream; False when in doubt, which
    only costs a synthesized silent track."""
    probe = shutil.which("ffprobe")
    if not probe:
        return False
    try:
        r = subprocess.run([probe, "-v", "error", "-select_streams", "a", "-show_entries",
                            "stream=codec_type", "-of", "csv=p=0", str(src)],
                           capture_output=True, text=True, timeout=60, check=False)
    except (subprocess.SubprocessError, OSError):
        return False
    return "audio" in (r.stdout or "")


def ffmpeg_command(src, out_dir, *, audio, still=False, duration=None):
    """The one profile every station segment shares (see module docstring)."""
    seg = config.STATION_SEGMENT_SECONDS
    br = config.STATION_BITRATE_K
    cmd = [ffmpeg_path() or "ffmpeg", "-y", "-v", "error", "-nostdin"]
    if still:
        cmd += ["-loop", "1", "-framerate", "30", "-t", "%.3f" % float(duration or 5), "-i", str(src)]
    else:
        cmd += ["-i", str(src)]
    if audio:
        afilter, amap = "aresample=async=1,apad", ["-map", "0:a:0"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        afilter, amap = "anull", ["-map", "1:a:0"]
    vf = ("scale=1920:1080:force_original_aspect_ratio=decrease,"
          "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p")
    cmd += ["-map", "0:v:0"] + amap + [
        "-vf", vf, "-af", afilter, "-shortest",
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-level", "4.1",
        "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-b:v", "%dk" % br, "-maxrate", "%dk" % int(br * 1.125), "-bufsize", "%dk" % (br * 2),
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
        "-f", "segment", "-segment_time", str(seg), "-segment_format", "mpegts",
        "-segment_list", str(Path(out_dir) / "segments.m3u8"), "-segment_list_type", "m3u8",
        str(Path(out_dir) / "%03d.ts")]
    return cmd


def _run(cmd):
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    try:
        _, err = proc.communicate(timeout=config.STATION_CONFORM_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        _, err = proc.communicate()
        raise RuntimeError("ffmpeg timed out: %s" % (err or b"").decode("utf-8", "ignore")[-600:]) from exc
    if proc.returncode != 0:
        raise RuntimeError((err or b"").decode("utf-8", "ignore")[-600:]
                           or "ffmpeg exited %s" % proc.returncode)


def _segment_durations(list_path):
    out = []
    for line in Path(list_path).read_text(encoding="utf-8").splitlines():
        if line.startswith("#EXTINF:"):
            out.append(float(line[len("#EXTINF:"):].split(",")[0]))
    return out


def _conform_file(key, row_id, src, *, still=False, duration=None):
    """Transcode `src` into cache_dir()/key, landing atomically. Returns key."""
    root = cache_dir()
    final, part = root / key, root / (key + ".part")
    if (final / "index.json").exists():
        return key
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir(parents=True)
    try:
        st = os.stat(src)
        _run(ffmpeg_command(src, part, audio=(not still and has_audio(src)),
                            still=still, duration=duration))
        segs = _segment_durations(part / "segments.m3u8")
        if not segs:
            raise RuntimeError("no segments produced")
        (part / "segments.m3u8").unlink()
        index = {"id": row_id, "key": key, "source_mtime_ns": st.st_mtime_ns,
                 "source_size": st.st_size, "segments": segs,
                 "duration": round(sum(segs), 3), "conformed_at": time.time()}
        (part / "index.json").write_text(json.dumps(index), encoding="utf-8")
        shutil.rmtree(final, ignore_errors=True)
        part.rename(final)
    except Exception:
        shutil.rmtree(part, ignore_errors=True)
        raise
    return key


def conform_source(row_id, src, *, still=False, duration=None):
    """Conform one registry row's file; the key is the file's identity."""
    st = os.stat(src)
    return _conform_file(cache_key(row_id, st.st_mtime_ns, st.st_size), row_id, src,
                         still=still, duration=duration)


def eligible_rows(c):
    """Rows the station may air: on, healthy, a real file, a real duration."""
    return [dict(r) for r in c.execute(
        "SELECT id,type,kind,uri,duration,title FROM playables "
        "WHERE enabled=1 AND health='ok' AND type IN ('video','card','image') "
        "AND uri IS NOT NULL AND uri!='' AND duration>0 ORDER BY created_at").fetchall()]


def expected_key(row):
    """(key, path) the row should be conformed under, or (None, None) if its file is gone."""
    src = paths.resolve_media(row["uri"])
    if not src or not src.is_file():
        return None, None
    st = src.stat()
    return cache_key(row["id"], st.st_mtime_ns, st.st_size), src


def load_index():
    """{id: index} for every landed entry; the newest wins if two keys share an id."""
    out = {}
    root = cache_dir()
    if not root.is_dir():
        return out
    for d in root.iterdir():
        f = d / "index.json"
        if not d.is_dir() or d.name.endswith(".part") or not f.is_file():
            continue
        try:
            idx = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if idx.get("key") != d.name:
            continue
        prev = out.get(idx.get("id"))
        if prev is None or idx.get("conformed_at", 0) > prev.get("conformed_at", 0):
            out[idx["id"]] = idx
    return out


def ensure_slate():
    """The built-in brand slate (Task 5). Stub until then."""
    return None


def sweep(limit=None, keep=frozenset()):
    """Conform what is missing, prune what is stale. Safe to run any time.

    `keep` is the set of keys some channel currently has on its timeline;
    they survive pruning even if their row has since been disabled, because
    yanking a segment out from under a live playlist is worse than airing a
    just-disabled item once more.
    """
    stats = {"conformed": 0, "failed": 0, "pruned": 0, "skipped": 0, "ffmpeg": bool(ffmpeg_path())}
    if not _LOCK.acquire(blocking=False):
        stats["busy"] = True
        return stats
    try:
        with db.conn() as c:
            rows = eligible_rows(c)
        current = {}
        for row in rows:
            key, src = expected_key(row)
            if key:
                current[row["id"]] = (key, src, row)
            else:
                stats["skipped"] += 1
        have = load_index()
        if stats["ffmpeg"]:
            todo = [(k, src, row) for k, src, row in current.values()
                    if have.get(row["id"], {}).get("key") != k]
            for k, src, row in (todo[:limit] if limit else todo):
                try:
                    conform_source(row["id"], src, still=(row["type"] == "image"),
                                   duration=row["duration"])
                    stats["conformed"] += 1
                except Exception as e:
                    stats["failed"] += 1
                    print("[station] conform failed for %s: %s" % (row["id"], e))
            try:
                ensure_slate()
            except Exception as e:
                print("[station] slate failed: %s" % e)
        else:
            print("[station] ffmpeg not found; nothing conformed")
        wanted = {k for k, _, _ in current.values()} | {SLATE_KEY} | set(keep)
        root = cache_dir()
        if root.is_dir():
            for d in root.iterdir():
                if not d.is_dir() or d.name in wanted:
                    continue
                if d.name.endswith(".part") and time.time() - d.stat().st_mtime < config.STATION_CONFORM_TIMEOUT:
                    continue
                shutil.rmtree(d, ignore_errors=True)
                stats["pruned"] += 1
        return stats
    finally:
        _LOCK.release()
```

- [ ] **Step 4: Run the conform tests**

Run: `python -m unittest tests.test_station_conform -v`
Expected: 12 tests, OK. If `test_conforms_eligible_prunes_stale_and_skips_streams` prunes 2, the slate directory does not exist yet and is not counted; the stale one is. If it conforms 3, check that `vid:off` (enabled=0) is excluded by `eligible_rows`.

- [ ] **Step 5: The loop and the lifespan**

Append to `bumparr/jobs.py`:

```python
async def station_conform_loop():
    """Keep the station's segment cache in step with the pool.

    Runs a sweep on startup and then every STATION_CONFORM_INTERVAL seconds.
    Nothing in this loop is in the playback path; the channels serve
    whatever is conformed and simply gain items as the sweep lands them.
    """
    from bumparr import config
    from bumparr.station import conform, playout
    interval = config.STATION_CONFORM_INTERVAL
    if interval <= 0:
        print("[station] conform loop disabled")
        return
    while True:
        try:
            stats = await asyncio.to_thread(conform.sweep, None, playout.active_keys())
            if stats.get("conformed") or stats.get("failed") or stats.get("pruned"):
                print("[station] conform: %s" % stats)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[station] conform loop error: %s" % e)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
```

`playout.active_keys()` does not exist until Task 6. For this task, create `bumparr/station/playout.py` containing only:

```python
"""Virtual-clock playout per channel (Task 6 fills this in)."""


def active_keys():
    """Keys some channel currently has on its timeline; none until Task 6."""
    return set()
```

In `bumparr/app.py` lifespan, add `asyncio.create_task(jobs.station_conform_loop())` to the `tasks` list.

- [ ] **Step 6: Full gate, then commit**

```bash
git add bumparr/station bumparr/jobs.py bumparr/app.py tests/test_station_conform.py
git commit -m "feat: station conform — one-time splice-safe segments per playable"
```

---

### Task 5: The slate

**Files:**
- Modify: `bumparr/station/conform.py` (`ensure_slate`)
- Test: `tests/test_station_conform.py` (append)

**Interfaces:**
- Consumes: `brandslam.draw(img, brand, face_path, size_px)`, `brandslam.static_face()`, `render_cards.W/H`, `ffmpeg_pipe.encode_frames`.
- Produces: `ensure_slate() -> "slate" | None` — a 10 s black-with-brand item conformed under `cache_dir()/slate/` with `index.json` `id="slate"`. None when ffmpeg is absent.

- [ ] **Step 1: Failing test**

```python
class Slate(Base):
    def test_slate_is_rendered_then_conformed_once(self):
        encoded = {"n": 0}
        def fake_encode(cmd, frames, dest, *, timeout, tail=600):
            encoded["n"] += 1
            list(frames)
            Path(dest).write_bytes(b"mp4")
        from bumparr import ffmpeg_pipe
        with self.popen(), mock.patch.object(ffmpeg_pipe, "encode_frames", side_effect=fake_encode), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"):
            self.assertEqual(conform.ensure_slate(), "slate")
            self.assertEqual(conform.ensure_slate(), "slate")
        self.assertEqual(encoded["n"], 1)
        idx = json.loads((conform.cache_dir() / "slate" / "index.json").read_text())
        self.assertEqual(idx["id"], "slate")
        self.assertFalse((conform.cache_dir() / "slate.mp4").exists())

    def test_slate_needs_ffmpeg(self):
        with mock.patch.object(conform, "ffmpeg_path", return_value=None):
            self.assertIsNone(conform.ensure_slate())
```

Run: `python -m unittest tests.test_station_conform.Slate -v` → FAIL (`ensure_slate` returns None / no index).

- [ ] **Step 2: Implement**

Replace the stub in `conform.py`:

```python
def ensure_slate():
    """The built-in brand slate: what airs when nothing else is conformed.

    Ten seconds of black with the brand, drawn with the same primitives as a
    station ident, so an empty pool still yields a stream that plays instead
    of a 404 that Dispatcharr treats as a dead channel. Rendered to an MP4
    through the shared frame pipe, then conformed like any other item.
    """
    root = cache_dir()
    if (root / SLATE_KEY / "index.json").exists():
        return SLATE_KEY
    if not ffmpeg_path():
        return None
    from PIL import Image

    from bumparr import brandslam, ffmpeg_pipe, render_cards
    root.mkdir(parents=True, exist_ok=True)
    mp4 = root / "slate.mp4"
    fps, seconds = 30, 10
    plate = Image.new("RGB", (render_cards.W, render_cards.H), "black")
    face = brandslam.static_face()
    frame = brandslam.draw(plate, config.BRAND, face, 13.0 * (min(render_cards.W, render_cards.H) / 100.0))
    data = frame.tobytes()
    cmd = [ffmpeg_path(), "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (render_cards.W, render_cards.H),
           "-r", str(fps), "-i", "-",
           "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", str(fps),
           "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(mp4)]
    try:
        ffmpeg_pipe.encode_frames(cmd, (data for _ in range(fps * seconds)), mp4, timeout=120)
        return _conform_file(SLATE_KEY, SLATE_KEY, mp4, still=False)
    finally:
        try:
            mp4.unlink()
        except OSError:
            pass
```

`brandslam.static_face()` returns None when no fonts are mounted; `brandslam.draw` handles a None face (it does for station IDs). Confirm by reading `brandslam.draw`; if it does not, guard with `if face is None: face = None` is not enough — instead skip the text and render plain black, and say so in a comment.

- [ ] **Step 3: Run, gate, commit**

Run: `python -m unittest tests.test_station_conform -v` → 14 tests OK.

```bash
git add bumparr/station/conform.py tests/test_station_conform.py
git commit -m "feat: station slate for an empty pool"
```

---

### Task 6: Playout

**Files:**
- Create (replace the Task 4 stub): `bumparr/station/playout.py`
- Test: `tests/test_station_playout.py`

**Interfaces:**
- Consumes: `conform.load_index()`, `rotation.weights_for(rows, season, now, daypart)`, `seasons.factors_now()`, `dayparts.factors_now()`, `db.conn()`, `config.STATION_SEGMENT_SECONDS`, `config.STATION_WINDOW_SEGMENTS`, `config.STANDBY_KINDS`, `config.BRAND`.
- Produces:
  - `Entry(start, item_id, key, segments, duration, title, kind)` dataclass with `.end`
  - `Channel(name, kinds=None, now=None, rng=None)` with `.advance(now)`, `.playlist(now, seg_url) -> str | None`, `.snapshot(now) -> {"now": …, "next": …}`, `.active_keys() -> set`, `.lookahead`
  - `get(name, now=None) -> Channel | None` (creates `live`/`standby` lazily), `active_keys() -> set`, `reset()` (tests)

- [ ] **Step 1: Failing tests**

```python
# tests/test_station_playout.py
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bumparr import config, db
from bumparr.station import conform, playout


def idx(item_id, key, segs):
    return {"id": item_id, "key": key, "segments": list(segs), "duration": round(sum(segs), 3), "conformed_at": 1}


INDEX = {"a": idx("a", "k-a", [4.0, 4.0, 2.0]),
         "b": idx("b", "k-b", [4.0, 1.5]),
         "c": idx("c", "k-c", [4.0, 4.0, 4.0, 4.0])}

URL = lambda key, n: "http://x/station/seg/%s/%03d.ts" % (key, n)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.originals = config.DB_PATH, config.ASSET_ROOT, config.OUTPUT_DIR
        config.DB_PATH = str(Path(self.tmp.name) / "p.db")
        config.ASSET_ROOT = Path(self.tmp.name) / "assets"; config.OUTPUT_DIR = config.ASSET_ROOT / "bumpers"
        config.ASSET_ROOT.mkdir(); config.OUTPUT_DIR.mkdir()
        for attr, value in zip(("DB_PATH", "ASSET_ROOT", "OUTPUT_DIR"), self.originals):
            self.addCleanup(setattr, config, attr, value)
        db.init_db()
        with db.conn() as c:
            for i, kind in (("a", "ambient"), ("b", "station_id"), ("c", "trivia")):
                c.execute("INSERT INTO playables (id,type,kind,uri,duration,title) VALUES (?,?,?,?,?,?)",
                          (i, "video", kind, "bumpers/%s.mp4" % i, 10, "Title " + i))
            c.commit()
        playout.reset(); self.addCleanup(playout.reset)
        self.patch = mock.patch.object(conform, "load_index", return_value=dict(INDEX)); self.patch.start()
        self.addCleanup(self.patch.stop)

    def channel(self, name="live", kinds=None, now=1000.0, seed=1):
        return playout.Channel(name, kinds, now, random.Random(seed))


class Timeline(Base):
    def test_extends_to_cover_the_lookahead_and_never_repeats_adjacent(self):
        ch = self.channel()
        ch.advance(1000.0)
        self.assertGreaterEqual(ch.timeline[-1].end, 1000.0 + ch.lookahead)
        self.assertEqual(ch.timeline[0].start, 1000.0)
        for e1, e2 in zip(ch.timeline, ch.timeline[1:]):
            self.assertEqual(e1.end, e2.start); self.assertNotEqual(e1.item_id, e2.item_id)

    def test_kinds_filter_restricts_the_pool(self):
        ch = self.channel("standby", {"station_id"})
        ch.advance(1000.0)
        self.assertTrue(all(e.item_id == "b" for e in ch.timeline))

    def test_empty_pool_airs_the_slate_or_nothing(self):
        with mock.patch.object(conform, "load_index", return_value={"slate": idx("slate", "slate", [4.0, 4.0, 2.0])}):
            ch = self.channel("standby", {"station_id"}); ch.advance(1000.0)
            self.assertTrue(ch.timeline and all(e.item_id == "slate" for e in ch.timeline))
        with mock.patch.object(conform, "load_index", return_value={}):
            ch = self.channel(); self.assertIsNone(ch.playlist(1000.0, URL))

    def test_stale_timeline_restarts_from_now_and_keeps_sequence_monotonic(self):
        ch = self.channel(); ch.advance(1000.0)
        seq_before = ch.seq_base; n = sum(len(e.segments) for e in ch.timeline)
        ch.advance(5000.0)
        self.assertEqual(ch.timeline[0].start, 5000.0)
        self.assertGreaterEqual(ch.seq_base, seq_before + n)


class Reporting(Base):
    def rows(self):
        with db.conn() as c:
            plays = dict(c.execute("SELECT id, play_count FROM playables").fetchall())
            hist = c.execute("SELECT playable_id, played_at FROM play_history WHERE channel_id='station:live' ORDER BY played_at").fetchall()
            cur = c.execute("SELECT current_id, started_at FROM playout WHERE channel_id='station:live'").fetchone()
        return plays, [tuple(h) for h in hist], (tuple(cur) if cur else None)

    def test_reports_each_started_entry_exactly_once(self):
        ch = self.channel(); ch.advance(1000.0)
        plays, hist, cur = self.rows()
        self.assertEqual(hist, [(ch.timeline[0].item_id, 1000.0)]); self.assertEqual(sum(plays.values()), 1)
        self.assertEqual(cur, (ch.timeline[0].item_id, 1000.0))
        ch.advance(1000.0); self.assertEqual(len(self.rows()[1]), 1)
        t = ch.timeline[1].start; ch.advance(t)
        plays, hist, cur = self.rows()
        self.assertEqual(len(hist), 2); self.assertEqual(cur, (ch.timeline[1].item_id, t))
        with db.conn() as c:
            lp = c.execute("SELECT last_played FROM playables WHERE id=?", (ch.timeline[1].item_id,)).fetchone()[0]
        self.assertEqual(lp, t)

    def test_never_writes_weight(self):
        ch = self.channel(); ch.advance(1000.0); ch.advance(1100.0)
        with db.conn() as c:
            self.assertEqual({r[0] for r in c.execute("SELECT weight FROM playables")}, {1.0})


class Playlist(Base):
    def test_window_shape_and_discontinuities(self):
        ch = self.channel(); body = ch.playlist(1000.0, URL)
        lines = body.splitlines()
        self.assertEqual(lines[:2], ["#EXTM3U", "#EXT-X-VERSION:3"])
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:0", lines); self.assertIn("#EXT-X-DISCONTINUITY-SEQUENCE:0", lines)
        self.assertNotIn("#EXT-X-ENDLIST", lines)
        first = ch.timeline[0]
        self.assertEqual(lines[lines.index("#EXT-X-DISCONTINUITY") + 1], "#EXTINF:%.3f," % first.segments[0])
        self.assertIn(URL(first.key, 0), lines)
        segs = [l for l in lines if l.endswith(".ts")]
        self.assertGreaterEqual(len(segs), config.STATION_WINDOW_SEGMENTS)
        target = int([l for l in lines if l.startswith("#EXT-X-TARGETDURATION:")][0].split(":")[1])
        self.assertGreaterEqual(target, 4)

    def test_sequence_numbers_advance_with_time(self):
        ch = self.channel(); ch.playlist(1000.0, URL)
        first = ch.timeline[0]
        # One segment length past the first entry's end: nothing of it can be in the window.
        later = ch.playlist(first.end + config.STATION_SEGMENT_SECONDS + 0.1, URL).splitlines()
        seq = int([l for l in later if l.startswith("#EXT-X-MEDIA-SEQUENCE:")][0].split(":")[1])
        dseq = int([l for l in later if l.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:")][0].split(":")[1])
        self.assertEqual(seq, len(first.segments))
        self.assertEqual(dseq, 1)
        self.assertNotIn(first.key, "\n".join(later))

    def test_mid_entry_window_counts_the_hidden_discontinuity(self):
        ch = self.channel(); ch.playlist(1000.0, URL)
        e1 = ch.timeline[1]
        # A window opening after the second entry's first segment has fully aged out:
        # both entry 0's tag and entry 1's own tag precede the playlist.
        t = e1.start + e1.segments[0] + config.STATION_SEGMENT_SECONDS + 0.1
        lines = ch.playlist(t, URL).splitlines()
        dseq = int([l for l in lines if l.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:")][0].split(":")[1])
        self.assertEqual(dseq, 2)


class Snapshot(Base):
    def test_now_and_next(self):
        ch = self.channel(); s = ch.snapshot(1000.0)
        self.assertEqual(s["now"]["id"], ch.timeline[0].item_id)
        self.assertEqual(s["now"]["started_at"], 1000.0); self.assertEqual(s["now"]["title"], "Title " + ch.timeline[0].item_id)
        self.assertEqual(s["next"]["id"], ch.timeline[1].item_id)
        self.assertEqual(ch.active_keys(), {e.key for e in ch.timeline})

    def test_registry_creates_known_channels_only(self):
        self.assertIsNone(playout.get("nope"))
        live = playout.get("live", 1000.0); self.assertIs(live, playout.get("live"))
        self.assertEqual(playout.get("standby", 1000.0).kinds, set(config.STANDBY_KINDS))
        live.advance(1000.0)
        self.assertTrue(playout.active_keys())


if __name__ == "__main__":
    unittest.main()
```

Run: `python -m unittest tests.test_station_playout -v` → errors (stub module).

- [ ] **Step 2: Implement `bumparr/station/playout.py`**

```python
"""A channel as a virtual clock.

There is no encoder loop and no thread. Each channel keeps a timeline of
(start time, conformed item) that is extended whenever someone asks for the
playlist, anchored to the wall clock, so serving the channel is arithmetic
over a list plus a static file per segment. The choice of what comes next
goes through the same rotation model as /api/bumpers/random, with the one
rule that model does not carry: never the same item twice in a row.

This is also the first thing in Bumparr that reports plays. When an entry's
start time passes, it is written to play_history and the row's last_played
and play_count move, which is what finally gives the recency, affinity and
fatigue factors something to work with. Nothing here touches `weight`.

If nobody asks for a playlist for longer than the window, the timeline is
stale and restarts from now: extending it through hours nobody watched would
report plays that never aired.
"""
import math
import random
import threading
import time
from dataclasses import dataclass

from bumparr import config, dayparts, db, rotation, seasons
from bumparr.station import conform

SLATE_ID = "slate"


@dataclass
class Entry:
    start: float
    item_id: str
    key: str
    segments: list
    duration: float
    title: str
    kind: str

    @property
    def end(self):
        return self.start + self.duration


class Channel:
    def __init__(self, name, kinds=None, now=None, rng=None):
        self.name = name
        self.kinds = set(kinds) if kinds else None
        self.rng = rng or random.Random()
        self.timeline = []
        self.seq_base = 0        # segments that have rolled off the front
        self.disc_base = 0       # entries that have rolled off (one discontinuity each)
        self.reported = 0        # entries from the front already written to the DB
        self.epoch = time.time() if now is None else now
        self.lock = threading.RLock()

    @property
    def lookahead(self):
        return config.STATION_WINDOW_SEGMENTS * config.STATION_SEGMENT_SECONDS

    def _pool(self, index):
        if not index:
            return []
        with db.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id,kind,title,weight,last_played,play_count FROM playables "
                "WHERE enabled=1 AND health='ok'").fetchall()]
        pool = []
        for r in rows:
            entry = index.get(r["id"])
            if not entry or (self.kinds is not None and r["kind"] not in self.kinds):
                continue
            r["_idx"] = entry
            pool.append(r)
        return pool

    def _pick(self, now, prev_id):
        index = conform.load_index()
        pool = self._pool(index)
        candidates = [r for r in pool if r["id"] != prev_id] or pool
        if not candidates:
            slate = index.get(SLATE_ID)
            if not slate:
                return None
            return {"id": SLATE_ID, "kind": SLATE_ID, "title": config.BRAND, "_idx": slate}
        try:
            season = seasons.factors_now()
        except Exception:
            season = {}
        try:
            daypart = dayparts.factors_now()
        except Exception:
            daypart = {}
        weights, _ = rotation.weights_for(candidates, season, now, daypart)
        weights = [max(1e-4, w) for w in weights]
        return self.rng.choices(candidates, weights=weights, k=1)[0]

    def _drop(self, n):
        for e in self.timeline[:n]:
            self.seq_base += len(e.segments)
            self.disc_base += 1
        del self.timeline[:n]
        self.reported = max(0, self.reported - n)

    def _report(self, now):
        started = [e for e in self.timeline[self.reported:] if e.start <= now]
        if not started:
            return
        with db.conn() as c:
            for e in started:
                if e.item_id == SLATE_ID:
                    continue
                c.execute("INSERT INTO play_history(channel_id, playable_id, played_at) VALUES (?,?,?)",
                          ("station:" + self.name, e.item_id, e.start))
                c.execute("UPDATE playables SET last_played=?, play_count=play_count+1 WHERE id=?",
                          (e.start, e.item_id))
            last = started[-1]
            c.execute("INSERT INTO playout(channel_id, current_id, started_at) VALUES (?,?,?) "
                      "ON CONFLICT(channel_id) DO UPDATE SET current_id=excluded.current_id, "
                      "started_at=excluded.started_at",
                      ("station:" + self.name, last.item_id, last.start))
            c.commit()
        self.reported += len(started)

    def advance(self, now):
        """Extend the timeline to cover now + lookahead; report; forget the distant past."""
        with self.lock:
            if self.timeline and now > self.timeline[-1].end + self.lookahead:
                self._drop(len(self.timeline))
            end = self.timeline[-1].end if self.timeline else now
            prev = self.timeline[-1].item_id if self.timeline else None
            while end < now + self.lookahead:
                pick = self._pick(end, prev)
                if pick is None:
                    break
                i = pick["_idx"]
                self.timeline.append(Entry(end, pick["id"], i["key"], list(i["segments"]),
                                           float(i["duration"]), pick.get("title") or "",
                                           pick.get("kind") or ""))
                end, prev = self.timeline[-1].end, pick["id"]
            self._report(now)
            n = 0
            while n < len(self.timeline) and self.timeline[n].end < now - 2 * self.lookahead:
                n += 1
            self._drop(n)

    def _segments(self):
        """Flatten to (global_index, entry, seg_index, seg_start, seg_duration)."""
        out, g = [], self.seq_base
        for e in self.timeline:
            t = e.start
            for i, d in enumerate(e.segments):
                out.append((g, e, i, t, d))
                g += 1
                t += d
        return out

    def playlist(self, now, seg_url):
        """The sliding-window media playlist, or None when nothing can air."""
        self.advance(now)
        with self.lock:
            seg = config.STATION_SEGMENT_SECONDS
            window = [s for s in self._segments()
                      if s[3] + s[4] > now - seg and s[3] < now + self.lookahead]
            if not window:
                return None
            first = window[0]
            # Every entry carries one discontinuity tag before its first segment.
            # Tags that precede the window, including the current entry's own if
            # the window opens mid-entry, are accounted for in the sequence number.
            disc_seq = self.disc_base + self.timeline.index(first[1]) + (1 if first[2] > 0 else 0)
            lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                     "#EXT-X-TARGETDURATION:%d" % math.ceil(max(s[4] for s in window)),
                     "#EXT-X-MEDIA-SEQUENCE:%d" % first[0],
                     "#EXT-X-DISCONTINUITY-SEQUENCE:%d" % disc_seq]
            for _, e, i, _, d in window:
                if i == 0:
                    lines.append("#EXT-X-DISCONTINUITY")
                lines.append("#EXTINF:%.3f," % d)
                lines.append(seg_url(e.key, i))
            return "\n".join(lines) + "\n"

    def snapshot(self, now):
        self.advance(now)
        with self.lock:
            cur = next((e for e in self.timeline if e.start <= now < e.end), None)
            nxt = next((e for e in self.timeline if e.start > now), None)

            def shape(e):
                return e and {"id": e.item_id, "title": e.title, "kind": e.kind,
                              "started_at": e.start, "ends_at": e.end}
            return {"now": shape(cur), "next": shape(nxt)}

    def active_keys(self):
        with self.lock:
            return {e.key for e in self.timeline}


CHANNELS = {}
_REGISTRY_LOCK = threading.Lock()


def kinds_for(name):
    return None if name == "live" else set(config.STANDBY_KINDS)


def get(name, now=None):
    """The named channel, created on first use; None for an unknown name."""
    if name not in ("live", "standby"):
        return None
    with _REGISTRY_LOCK:
        ch = CHANNELS.get(name)
        if ch is None:
            ch = CHANNELS[name] = Channel(name, kinds_for(name), now)
        return ch


def active_keys():
    """Every key some channel has on its timeline; the conform sweep spares these."""
    with _REGISTRY_LOCK:
        channels = list(CHANNELS.values())
    keys = set()
    for ch in channels:
        keys |= ch.active_keys()
    return keys


def reset():
    """Forget every channel (tests)."""
    with _REGISTRY_LOCK:
        CHANNELS.clear()
```

- [ ] **Step 3: Run the tests**

Run: `python -m unittest tests.test_station_playout -v`
Expected: 12 tests OK. The window rule is "a segment is in the window while it ended less than one segment length ago and starts before now + lookahead"; both sequence tests are timed one segment length past the boundary so the previous entry is fully out.

- [ ] **Step 4: Full gate, then commit**

```bash
git add bumparr/station/playout.py tests/test_station_playout.py
git commit -m "feat: station playout — virtual-clock channels that report plays"
```

---

### Task 7: Guide

**Files:**
- Create: `bumparr/station/guide.py`
- Test: `tests/test_station_guide.py`

**Interfaces:**
- Consumes: `dayparts.blocks`, `dayparts.now_local`, `config.BRAND`.
- Produces: `xmltv(now=None, brand=None, back_hours=6, ahead_hours=24) -> str`; channel ids `bumparr.live`, `bumparr.standby`.

- [ ] **Step 1: Failing test**

```python
# tests/test_station_guide.py
import datetime
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from bumparr import dayparts
from bumparr.station import guide

TZ = datetime.timezone(datetime.timedelta(hours=-4))
NOW = datetime.datetime(2026, 9, 5, 9, 30, tzinfo=TZ)
PARTS = "dayparts:\n  morning:\n    hours: \"06:00-10:00\"\n    description: \"Weather.\"\n"


class Guide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        p = Path(self.tmp.name) / "d.yaml"; p.write_text(PARTS, encoding="utf-8")
        self.parts = dayparts.load_dayparts(p)
        patcher = mock.patch.object(dayparts, "load_dayparts", return_value=self.parts)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_two_channels_and_contiguous_programmes(self):
        root = ET.fromstring(guide.xmltv(NOW, "TV"))
        self.assertEqual([c.get("id") for c in root.findall("channel")], ["bumparr.live", "bumparr.standby"])
        self.assertEqual(root.find("channel/display-name").text, "TV")
        live = [p for p in root.findall("programme") if p.get("channel") == "bumparr.live"]
        self.assertEqual(live[0].get("start"), "20260905033000 -0400")
        self.assertEqual(live[-1].get("stop"), "20260906093000 -0400")
        for a, b in zip(live, live[1:]):
            self.assertEqual(a.get("stop"), b.get("start"))
        morning = [p for p in live if p.find("title").text == "TV — morning"]
        self.assertTrue(morning); self.assertEqual(morning[0].find("desc").text, "Weather.")
        standby = [p for p in root.findall("programme") if p.get("channel") == "bumparr.standby"]
        self.assertTrue(standby)
        self.assertTrue(all(p.find("title").text == "TV — Please stand by" for p in standby))
        self.assertLessEqual(standby[0].get("start"), "20260905033000 -0400")
        self.assertGreaterEqual(standby[-1].get("stop"), "20260906093000 -0400")


if __name__ == "__main__":
    unittest.main()
```

Run: `python -m unittest tests.test_station_guide -v` → import error.

- [ ] **Step 2: Implement**

```python
"""XMLTV for the station's two channels.

Dispatcharr, Tunarr and most players show an empty grid for a channel with
no guide, and an empty grid reads as broken. Five-second items would be
noise in a grid, so the live channel's programmes are the daypart blocks:
the same windows that steer the rotation, with their viewer-facing
descriptions. Standby is one rolling "please stand by" programme, because
that is honestly all it is.
"""
import datetime
import xml.etree.ElementTree as ET

from bumparr import config, dayparts

LIVE_ID, STANDBY_ID = "bumparr.live", "bumparr.standby"


def _fmt(dt):
    return dt.strftime("%Y%m%d%H%M%S %z")


def xmltv(now=None, brand=None, back_hours=6, ahead_hours=24):
    """The guide as a string, covering now-back_hours .. now+ahead_hours."""
    brand = brand or config.BRAND
    t0 = dayparts.now_local(now)
    start, end = t0 - datetime.timedelta(hours=back_hours), t0 + datetime.timedelta(hours=ahead_hours)
    tv = ET.Element("tv", {"generator-info-name": "bumparr"})
    for cid, label in ((LIVE_ID, brand), (STANDBY_ID, brand + " standby")):
        ch = ET.SubElement(tv, "channel", id=cid)
        ET.SubElement(ch, "display-name").text = label
    for s, e, title, desc in dayparts.blocks(start, end, brand):
        p = ET.SubElement(tv, "programme", start=_fmt(s), stop=_fmt(e), channel=LIVE_ID)
        ET.SubElement(p, "title").text = title
        if desc:
            ET.SubElement(p, "desc").text = desc
    t = start.replace(minute=0, second=0, microsecond=0)
    t -= datetime.timedelta(hours=t.hour % 6)
    while t < end:
        e = t + datetime.timedelta(hours=6)
        p = ET.SubElement(tv, "programme", start=_fmt(t), stop=_fmt(e), channel=STANDBY_ID)
        ET.SubElement(p, "title").text = "%s — Please stand by" % brand
        ET.SubElement(p, "desc").text = "Standby loop: station IDs, test cards, and live windows."
        t = e
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(tv, encoding="unicode") + "\n"
```

- [ ] **Step 3: Run, gate, commit**

```bash
git add bumparr/station/guide.py tests/test_station_guide.py
git commit -m "feat: station XMLTV guide from daypart blocks"
```

---

### Task 8: Routes, the status endpoint, and the conform action

**Files:**
- Create: `bumparr/station/routes.py`
- Modify: `bumparr/app.py` (include router; `/station/seg` mount; `POST /api/station/conform`)
- Modify: `requirements-dev.txt` (add `httpx==0.27.2`)
- Modify: `docs/API.md`
- Test: `tests/test_station_routes.py`

**Interfaces:**
- Consumes: `urls.public_base`, `playout.get`, `guide.xmltv`, `conform.load_index/eligible_rows/ffmpeg_path`, `app._start_job`.
- Produces the routes in spec §3. `GET /api/station` JSON shape:

```json
{"ffmpeg": true, "conformed": 12, "eligible": 14, "pending": 2,
 "urls": {"channel_m3u": "…/station/channel.m3u", "guide_xml": "…/station/guide.xml",
          "live": "…/station/live/index.m3u8", "standby": "…/station/standby/index.m3u8"},
 "channels": {"live": {"now": {...}|null, "next": {...}|null}, "standby": {...}}}
```

- [ ] **Step 1: Dev dependency**

Append `httpx==0.27.2` to `requirements-dev.txt` (Starlette's `TestClient` needs it; CI installs this file). Install it locally: `pip install -r requirements-dev.txt`.

- [ ] **Step 2: Failing tests**

```python
# tests/test_station_routes.py
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from bumparr import config, db
from bumparr.app import app
from bumparr.station import conform, playout


def idx(item_id, key, segs):
    return {"id": item_id, "key": key, "segments": list(segs), "duration": round(sum(segs), 3), "conformed_at": 1}


class Routes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.originals = config.DB_PATH, config.ASSET_ROOT, config.OUTPUT_DIR, config.PUBLIC_BASE_URL
        config.DB_PATH = str(Path(self.tmp.name) / "r.db")
        config.ASSET_ROOT = Path(self.tmp.name) / "assets"; config.OUTPUT_DIR = config.ASSET_ROOT / "bumpers"
        config.ASSET_ROOT.mkdir(); config.OUTPUT_DIR.mkdir()
        config.PUBLIC_BASE_URL = "http://bumparr.example:8780"
        for attr, value in zip(("DB_PATH", "ASSET_ROOT", "OUTPUT_DIR", "PUBLIC_BASE_URL"), self.originals):
            self.addCleanup(setattr, config, attr, value)
        db.init_db()
        with db.conn() as c:
            c.execute("INSERT INTO playables (id,type,kind,uri,duration,title) VALUES (?,?,?,?,?,?)",
                      ("a", "video", "station_id", "bumpers/a.mp4", 10, "Ident"))
            c.commit()
        playout.reset(); self.addCleanup(playout.reset)
        self.client = TestClient(app)

    def test_playlist_is_live_and_absolute(self):
        with mock.patch.object(conform, "load_index", return_value={"a": idx("a", "k-a", [4.0, 4.0, 2.0])}):
            r = self.client.get("/station/live/index.m3u8")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"].split(";")[0], "application/vnd.apple.mpegurl")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertIn("http://bumparr.example:8780/station/seg/k-a/000.ts", r.text)
        self.assertNotIn("#EXT-X-ENDLIST", r.text)

    def test_unknown_channel_and_empty_cache(self):
        self.assertEqual(self.client.get("/station/nope/index.m3u8").status_code, 404)
        with mock.patch.object(conform, "load_index", return_value={}):
            r = self.client.get("/station/standby/index.m3u8")
        self.assertEqual(r.status_code, 503); self.assertIn("conform", r.text)

    def test_channel_m3u_and_guide(self):
        r = self.client.get("/station/channel.m3u")
        self.assertIn('tvg-id="bumparr.live"', r.text); self.assertIn('tvg-id="bumparr.standby"', r.text)
        self.assertIn("http://bumparr.example:8780/station/standby/index.m3u8", r.text)
        g = self.client.get("/station/guide.xml")
        self.assertEqual(g.status_code, 200); self.assertIn('channel id="bumparr.live"', g.text)
        self.assertEqual(g.headers["content-type"].split(";")[0], "application/xml")

    def test_status_shape(self):
        with mock.patch.object(conform, "load_index", return_value={"a": idx("a", "k-a", [4.0]), "slate": idx("slate", "slate", [4.0])}), \
                mock.patch.object(conform, "ffmpeg_path", return_value=None):
            s = self.client.get("/api/station").json()
        self.assertEqual((s["ffmpeg"], s["conformed"], s["eligible"], s["pending"]), (False, 1, 1, 0))
        self.assertEqual(s["urls"]["channel_m3u"], "http://bumparr.example:8780/station/channel.m3u")
        self.assertEqual(s["channels"]["live"]["now"]["id"], "a")
        self.assertIn("standby", s["channels"])

    def test_conform_action_is_a_job(self):
        with mock.patch.object(conform, "sweep", return_value={"conformed": 0}) as sweep:
            r = self.client.post("/api/station/conform?limit=5")
            self.assertEqual(r.status_code, 200); job = r.json()["job_id"]
            for _ in range(50):
                st = self.client.get("/api/request/" + job).json()
                if st["status"] != "working":
                    break
        self.assertEqual(st["status"], "done"); sweep.assert_called_once()
        self.assertEqual(sweep.call_args.kwargs.get("limit"), 5)


if __name__ == "__main__":
    unittest.main()
```

`eligible_rows` filters on registry fields only (the file check happens in `expected_key`), so the row counts as eligible even though `bumpers/a.mp4` is not on disk; the slate key is excluded from `conformed`.

Run: `python -m unittest tests.test_station_routes -v` → import error.

- [ ] **Step 3: Create `bumparr/station/routes.py`**

```python
"""The station's HTTP surface: playlists, the channel list, the guide, status."""
import time

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from bumparr import config, db, urls
from bumparr.station import conform, guide, playout

router = APIRouter()
M3U8 = "application/vnd.apple.mpegurl"


def _seg_url(request):
    base = urls.public_base(request)
    return lambda key, n: "%s/station/seg/%s/%03d.ts" % (base, key, n)


def _attr(s):
    return str(s or "").replace('"', "'").replace("\n", " ")


@router.get("/station/{channel}/index.m3u8")
def index_m3u8(channel: str, request: Request):
    """The live media playlist. `no-store` because it changes every few seconds."""
    ch = playout.get(channel)
    if ch is None:
        return PlainTextResponse("unknown channel\n", status_code=404)
    body = ch.playlist(time.time(), _seg_url(request))
    if body is None:
        return PlainTextResponse("nothing conformed yet; run a conform pass (POST /api/station/conform)\n",
                                 status_code=503, headers={"Cache-Control": "no-store"})
    return Response(body, media_type=M3U8, headers={"Cache-Control": "no-store"})


@router.get("/station/channel.m3u")
def channel_m3u(request: Request):
    """Both channels as an M3U source, tvg-ids matching guide.xml."""
    base, brand = urls.public_base(request), config.BRAND
    lines = ["#EXTM3U"]
    for name, label in (("live", brand), ("standby", brand + " standby")):
        lines.append('#EXTINF:-1 tvg-id="bumparr.%s" tvg-name="%s" group-title="Bumparr",%s'
                     % (name, _attr(label), _attr(label)))
        lines.append("%s/station/%s/index.m3u8" % (base, name))
    return PlainTextResponse("\n".join(lines) + "\n", media_type="audio/x-mpegurl")


@router.get("/station/guide.xml")
def guide_xml():
    return Response(guide.xmltv(), media_type="application/xml",
                    headers={"Cache-Control": "max-age=300"})


@router.get("/api/station")
def station_status(request: Request):
    """Now/next per channel, conform progress, and the URLs to hand a consumer."""
    now, base = time.time(), urls.public_base(request)
    index = conform.load_index()
    with db.conn() as c:
        eligible = len(conform.eligible_rows(c))
    conformed = len([k for k in index if k != conform.SLATE_KEY])
    return {"ffmpeg": bool(conform.ffmpeg_path()), "conformed": conformed, "eligible": eligible,
            "pending": max(0, eligible - conformed),
            "urls": {"channel_m3u": base + "/station/channel.m3u",
                     "guide_xml": base + "/station/guide.xml",
                     "live": base + "/station/live/index.m3u8",
                     "standby": base + "/station/standby/index.m3u8"},
            "channels": {name: playout.get(name).snapshot(now) for name in ("live", "standby")}}
```

- [ ] **Step 4: Wire `app.py`**

Imports: `from bumparr.station import routes as station_routes` and `from bumparr.station import conform as station_conform, playout as station_playout` (inside the endpoint is fine too).

After `app.include_router(stream_proxy.router)` add `app.include_router(station_routes.router)`.

Beside the other mounts (before `/media`):

```python
# Conformed station segments. Served straight from the cache: the playlist
# is arithmetic and the segment is a file, which is the whole point.
app.mount("/station/seg",
          StaticFiles(directory=str(config.ASSET_ROOT / ".cache" / "station"), check_dir=False),
          name="station-segments")
```

Beside `/api/render/cards`:

```python
@app.post("/api/station/conform")
async def station_conform(limit: int = Query(25, ge=1, le=1000)):
    """Prepare the pool for the live channels: conform up to `limit` items now.

    The background loop does this on its own; the action exists so a fresh
    install can see the channel fill without waiting for the interval.
    """
    from bumparr.station import conform, playout

    def work():
        return conform.sweep(limit=limit, keep=playout.active_keys())
    return _start_job("station conform", work)
```

- [ ] **Step 5: Run, then document in `docs/API.md`**

Run: `python -m unittest tests.test_station_routes -v` → 5 tests OK.

Add a `## Station` section to `docs/API.md` listing the six routes with one line each, the `/api/station` JSON shape above, the 503 meaning, and that `PUBLIC_URL` must be reachable from the consumer.

- [ ] **Step 6: Full gate, then commit**

```bash
git add bumparr/station/routes.py bumparr/app.py requirements-dev.txt docs/API.md tests/test_station_routes.py
git commit -m "feat: station routes — HLS playlists, channel M3U, guide, status, conform action"
```

---

### Task 9: Dashboard panel

**Files:**
- Modify: `bumparr/web/index.html` (new panel after Pool)
- Modify: `bumparr/web/app.js` (`stationEl`, `loadStation`, wiring, export)
- Modify: `bumparr/web/style.css`
- Test: `bumparr/web/app.test.js`

**Interfaces:**
- Consumes: `GET /api/station`, `POST /api/station/conform` (via `doAction`).
- Produces: `stationEl(status) -> node` exported for tests.

- [ ] **Step 1: Failing Node test** (append to `app.test.js`; the fixture exports need `stationEl` added to the `require`)

```javascript
test("station panel renders now/next and URLs as text and values, never markup", () => {
  const s = {
    ffmpeg: true, conformed: 3, eligible: 5,
    urls: { channel_m3u: "http://x/station/channel.m3u\"><img src=x>", guide_xml: "http://x/g.xml", standby: "http://x/s.m3u8" },
    channels: {
      live: { now: { id: "a", title: "<b>Ident</b>", kind: "station_id", started_at: 0, ends_at: Date.now() / 1000 + 5 }, next: { id: "b", title: "Next & co", kind: "trivia" } },
      standby: { now: null, next: null },
    },
  };
  const el = stationEl(s);
  const text = JSON.stringify(el);
  assert.ok(text.includes("<b>Ident</b>"));
  assert.ok(!text.includes("innerHTML"));
  const inputs = [];
  (function walk(n) { if (n.tagName === "INPUT") inputs.push(n); (n.children || []).forEach(walk); })(el);
  assert.equal(inputs.length, 3);
  assert.ok(inputs[0].value.includes("<img src=x>"));
  assert.ok(inputs.every((i) => i.readOnly === true));
  assert.ok(text.includes("off air"));
  assert.ok(text.includes("3 / 5 conformed"));
});

test("station panel says when ffmpeg is missing", () => {
  const el = stationEl({ ffmpeg: false, conformed: 0, eligible: 4, urls: {}, channels: {} });
  assert.ok(JSON.stringify(el).includes("ffmpeg not found"));
});
```

Run: `node --test bumparr/web/app.test.js` → fails (`stationEl is not a function`).

- [ ] **Step 2: `index.html`**

After the Pool `</section>` insert:

```html
    <section class="panel">
      <h2>Station</h2>
      <div id="station" class="station"></div>
      <div class="action-group">
        <button data-station="conform">Conform now <em>(prepare the pool for the live channels)</em></button>
      </div>
    </section>
```

- [ ] **Step 3: `app.js`**

Add after `cardEl`:

```javascript
function stationEl(s) {
  const root = makeEl("div", "station-body");
  for (const name of ["live", "standby"]) {
    const ch = (s.channels || {})[name] || {};
    const row = makeEl("div", "station-row");
    row.append(makeEl("span", "lbl", name));
    if (ch.now) {
      const left = Math.max(0, Math.round((ch.now.ends_at || 0) - Date.now() / 1000));
      row.append(makeEl("span", "now", ch.now.title + " (" + ch.now.kind + ", " + left + "s left)"));
    } else {
      row.append(makeEl("span", "now muted", "off air"));
    }
    if (ch.next) row.append(makeEl("span", "next muted", "next: " + ch.next.title));
    root.append(row);
  }
  const urls = s.urls || {};
  for (const [label, key] of [["Channel M3U", "channel_m3u"], ["Guide XMLTV", "guide_xml"], ["Standby HLS", "standby"]]) {
    const row = makeEl("div", "station-url");
    row.append(makeEl("span", "lbl", label));
    const input = document.createElement("input");
    input.readOnly = true; input.className = "url"; input.value = urls[key] || "";
    input.addEventListener("focus", () => input.select && input.select());
    row.append(input);
    root.append(row);
  }
  root.append(makeEl("div", "muted", s.ffmpeg === false
    ? "ffmpeg not found: nothing can be conformed"
    : (s.conformed || 0) + " / " + (s.eligible || 0) + " conformed"));
  return root;
}

async function loadStation() {
  let s;
  try { s = await (await fetch("/api/station")).json(); } catch (e) { return; }
  const el = $("#station");
  el.textContent = "";
  el.append(stationEl(s));
}
```

In `boot()`: wire `document.querySelectorAll("[data-station]")` to `doAction("/api/station/conform", "conform")`; call `loadStation()` and add `setInterval(loadStation, 20000)`. In `doAction`, after `loadStatus(); loadGrid(true);` add `loadStation();`. Export `stationEl` in `module.exports`.

The fake DOM in `app.test.js` gives `FakeNode` `append`, `className`, `textContent`, `dataset`, `style`; `document.createElement("input")` returns a `FakeNode` on which `readOnly`, `value`, and `addEventListener` work as plain properties/no-ops. No changes to the fixture are needed.

- [ ] **Step 4: `style.css`**

```css
.station-row, .station-url { display: flex; gap: .6rem; align-items: center; margin: .25rem 0; }
.station-row .lbl, .station-url .lbl { min-width: 6.5rem; }
.station-url input.url { flex: 1; font-family: monospace; font-size: .85rem; }
.station .muted { opacity: .7; }
```

- [ ] **Step 5: Gate and commit**

```bash
node --check bumparr/web/app.js && node --test bumparr/web/app.test.js
git add bumparr/web
git commit -m "feat: station panel on the dashboard"
```

---

### Task 10: Integration story

**Files:**
- Modify: `docs/INTEGRATION.md`, `README.md`, `docs/ARCHITECTURE.md`, `CHANGELOG.md`

- [ ] **Step 1: INTEGRATION.md**

Replace the Dispatcharr section with:

````markdown
## Dispatcharr

Dispatcharr relays live streams; it does not schedule files, so a playlist
of bumper files is not useful to it. Bumparr therefore runs its pool **as a
live channel**, and Dispatcharr consumes that like any provider.

1. **Add the channel.** Sources → M3U: `http://bumparr:8780/station/channel.m3u`.
   Two streams appear in the `Bumparr` group: the live channel and standby.
2. **Add the guide.** Sources → EPG: `http://bumparr:8780/station/guide.xml`.
   The `tvg-id`s match, so the guide assigns itself.
3. **Use standby as failover.** On any channel whose provider drops, add
   `http://bumparr:8780/station/standby/index.m3u8` as the **last** stream.
   Dispatcharr rotates onto it when everything above it fails, and the
   viewer sees a branded "please stand by" loop instead of a dead stream.

`PUBLIC_URL` must be the address Dispatcharr's container can reach, because
segment URLs in the playlist are absolute.

The channel's character by hour comes from `config_files/dayparts.yaml`;
what standby may air comes from `STANDBY_KINDS`. Until the first conform
sweep finishes the playlist returns 503; the dashboard's Station panel shows
progress and has a "Conform now" button.
````

Add to the ErsatzTV and Tunarr sections one paragraph each: the live channel can be added as a stream source (`/station/live/index.m3u8`) for a "Bumparr TV" channel alongside the file-based filler.

- [ ] **Step 2: README**

In "Consume it", add a row: `GET /station/channel.m3u` + `/station/guide.xml` | Bumparr as a live channel (Dispatcharr, or any HLS player); `/station/standby/index.m3u8` as a failover stream. One sentence under the table: "The station is the pool run as a live channel: pre-conformed segments and a playlist, no encoder in the playback path."

- [ ] **Step 3: ARCHITECTURE.md**

Add `station/` to the content-flow diagram (a consumer of the registry and a writer of play history), a "6. Station" subsection of four sentences (conform, playout, guide, routes), the file-map rows, and the invariant: **The station airs only conformed items, and conform never runs in the request path.**

- [ ] **Step 4: CHANGELOG**

Under Unreleased, a **Station** block:

```markdown
**Station**
- The pool now runs as two live HLS channels, `live` and `standby`, with a
  channel M3U and an XMLTV guide, so Dispatcharr can carry Bumparr as a
  channel and use standby as branded failover. Items are conformed once
  into splice-safe segments by a background job; nothing encodes at serve
  time.
- The playout is the first writer of play history: `last_played`,
  `play_count` and `play_history` now move, which wakes the recency,
  affinity and fatigue factors.
- Dayparts (`config_files/dayparts.yaml`): time-of-day windows as a
  rotation factor and as the guide's programme blocks.
- New settings: `STATION_*`, `STANDBY_KINDS` (see CONFIG.md).
```

- [ ] **Step 5: Commit**

```bash
git add docs/INTEGRATION.md README.md docs/ARCHITECTURE.md CHANGELOG.md
git commit -m "docs: Dispatcharr as a first-class consumer of the station"
```

---

### Task 11: Acceptance against a real Dispatcharr

**Files:**
- Modify: this plan (append an "Acceptance" section with the result)

This is operator work; the deliverable is a written result. Nothing merges as "works with Dispatcharr" without it.

- [ ] **Step 1: Run Bumparr with ffmpeg and let it conform**

```bash
docker compose up -d --build
curl -s http://localhost:8780/api/station | python3 -m json.tool | head -20
curl -X POST 'http://localhost:8780/api/station/conform?limit=50'
```

Wait until `pending` is 0 or small. Confirm `curl -s http://localhost:8780/station/live/index.m3u8` returns a playlist and `ffmpeg -i <PUBLIC_URL>/station/live/index.m3u8 -t 60 -f null -` runs for a full minute with no errors.

- [ ] **Step 2: Dispatcharr channel**

In Dispatcharr add the M3U and EPG sources from INTEGRATION.md, create a channel from the `live` stream, open it in Dispatcharr's web player and in one external client (VLC or Jellyfin). Watch for at least five item boundaries. Record: does audio stay in sync across boundaries; any freeze at a discontinuity; guide populated.

- [ ] **Step 3: Failover**

Take a channel with a real provider stream, append the standby stream last, then break the provider (wrong URL). Confirm Dispatcharr rotates to standby and the player shows the loop. Restore the provider.

- [ ] **Step 4: Record**

Append to this file:

```markdown
## Acceptance (date, Dispatcharr version, Bumparr commit)

- Live channel in Dispatcharr: PASS/FAIL — notes
- Boundaries watched: N; audio sync: …; freezes: …
- Guide: …
- Failover to standby: PASS/FAIL — notes
- Player(s): …
```

If a boundary freezes in Dispatcharr's proxy but not in ffmpeg directly, the fallback named in the spec applies: a single ffmpeg concat loop per channel writing HLS to the cache dir, with `playout` keeping its timeline as the source of the concat list. That is a new task, planned separately; do not improvise it here.

```bash
git add docs/superpowers/plans/2026-09-05-station-playout.md
git commit -m "docs: station acceptance result"
```

---

## Landing order

1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11, with 1–3 parallelisable and 7 parallelisable with 4–6. Each task ends with the full gate green.

## Acceptance

### Local, 2026-09-05, Bumparr at commit a0af6b9, ffmpeg 8.0.1

Steps 1 of Task 11, run against a scratch `ASSET_ROOT`/`DB_PATH` with the
app booted under uvicorn on 127.0.0.1:8791 and `PUBLIC_URL` set to match.

- Fresh pool: 58 playables (52 baseline cards, 6 configured cams), none
  rendered. `POST /api/render/cards?limit=6` rendered six; `/api/station`
  then reported `eligible: 6, pending: 6, ffmpeg: true`.
- Before any conform, both channels served the slate on loop (no 503 once
  the slate existed), which is the designed empty-pool behaviour.
- `POST /api/station/conform?limit=20` → `{'conformed': 6, 'failed': 0,
  'pruned': 0, 'skipped': 0}`; the cache held six keyed directories plus
  `slate`.
- `ffmpeg -i …/station/live/index.m3u8 -t 40 -f null -` ran the full 40 s
  and exited 0. The only stderr lines were the audio timestamp resets at
  entry joins the spec anticipates; no freezes, no missing segments.
- `play_history` gained rows for `station:live` with real card ids, and
  those rows' `last_played`/`play_count` moved. `weight` was untouched.
- `/station/standby/index.m3u8` served 200 with a seven-segment window.
- `/station/guide.xml` listed both channels with contiguous daypart blocks
  in the process's local zone; `/station/channel.m3u` carried both
  `tvg-id`s and absolute URLs.

**Local result: PASS.**

### Dispatcharr (Task 11 steps 2–4): PENDING, operator-run

Needs a reachable Dispatcharr instance. Follow the three steps in
`docs/INTEGRATION.md` under Dispatcharr, watch at least five item
boundaries in Dispatcharr's player and one external client, break a
provider stream with standby appended last, and record here: boundary
count, audio sync, freezes, guide population, failover PASS/FAIL, player(s).
