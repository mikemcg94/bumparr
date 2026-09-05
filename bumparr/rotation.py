"""The one scoring model. How likely a bumper is to play, right now.

This exists because weight was doing three jobs at once. It held editorial
intent, the seasonal pass overwrote it in the database, and the scheduler
layered further multipliers on at pick time — three systems writing to one
number, with a `base_weight` field bolted on to undo the damage. Any of them
could quietly cancel another out, and nothing could be reasoned about or tested
in isolation.

The split is between what is DECLARED and what is COMPUTED.

Declared, and never modified by the system:
    base        editorial intent — how much this material is wanted at all.

Computed fresh at every selection, never written back:
    season      is this appropriate for today's date
    daypart     does this kind belong to this hour of the day (dayparts.yaml)
    recency     did THIS clip just play — deep drop, slow recovery
    affinity    did something LIKE it just play — shallower drop, quicker recovery
    fatigue     has it been overplayed across its whole life

    score = base x season x daypart x recency x affinity x fatigue

Because nothing is written back, the model is idempotent, explainable after the
fact, and safe to change: adjusting a curve alters behaviour immediately without
migrating any stored state.

The curves are all of the form `t / (t + tau)`, which starts at zero, passes 0.5
at tau, and approaches 1 without ever quite arriving. That shape is deliberate:
recovery is fast at first and then slow, so a clip becomes eligible again
reasonably soon but does not fully reset for a long while — which is what makes
a rotation feel like a rotation rather than a shuffle.
"""
import time

from bumparr import config

# --- recency: this exact clip -------------------------------------------------
# The strongest signal. Just played means effectively out of the running, and it
# takes hours to come back — the single biggest cause of a large pool feeling
# small is an item returning too soon.
RECENCY_TAU = float(config.env("RECENCY_TAU", 3 * 3600))   # 0.5 at 3h
RECENCY_FLOOR = float(config.env("RECENCY_FLOOR", 0.015))

# --- affinity: something of the same KIND ------------------------------------
# Shallower and faster. Two traffic cams back to back is a texture problem, not
# a repetition problem, so the category steps aside briefly rather than leaving.
AFFINITY_TAU = float(config.env("AFFINITY_TAU", 25 * 60))  # 0.5 at 25m
AFFINITY_FLOOR = float(config.env("AFFINITY_FLOOR", 0.30))

# --- fatigue: lifetime overplay ----------------------------------------------
# Recency handles the last few hours; fatigue handles the last few weeks. Judged
# against the pool's own median so it scales with library size instead of a
# magic number: with a median of 4 plays, an item on 45 is plainly overexposed.
FATIGUE_STRENGTH = float(config.env("FATIGUE_STRENGTH", 0.5))
FATIGUE_MIN = float(config.env("FATIGUE_MIN", 0.25))
FATIGUE_MAX = float(config.env("FATIGUE_MAX", 1.6))


def _recover(age, tau, floor):
    """Recovery curve: `floor` at zero age, rising toward 1.0 with time."""
    if age is None or age <= 0:
        return floor
    return floor + (1.0 - floor) * (age / (age + tau))


def recency(last_played, now=None):
    """How far this clip has recovered since it last aired.

    Something never played returns 1.0 and therefore outranks everything of
    equal weight, which is what puts new material on air promptly instead of
    leaving it to win a lottery against a pool of hundreds.
    """
    if not last_played:
        return 1.0
    return _recover((now or time.time()) - last_played, RECENCY_TAU, RECENCY_FLOOR)


def affinity(kind_last_played, now=None):
    """How far this clip's CATEGORY has recovered since one of its own aired."""
    if not kind_last_played:
        return 1.0
    return _recover((now or time.time()) - kind_last_played, AFFINITY_TAU, AFFINITY_FLOOR)


def fatigue(play_count, median_plays):
    """Lifetime overplay, relative to the pool rather than an absolute count.

    Square-rooted so the penalty grows steadily instead of collapsing an item
    the moment it pulls ahead: at four times the median an item is roughly
    halved, not silenced.
    """
    med = max(1.0, float(median_plays or 1))
    plays = max(0.0, float(play_count or 0))
    f = ((med + 1.0) / (plays + 1.0)) ** FATIGUE_STRENGTH
    return max(FATIGUE_MIN, min(FATIGUE_MAX, f))


def score(item, ctx, now=None):
    """Combined score for one item.

    `item` needs base/weight, kind, last_played and play_count.
    `ctx` carries pool-wide state: {"kind_last": {kind: ts}, "median_plays": n,
    "season": {kind: factor}, "daypart": {kind: factor}}.
    """
    now = now or time.time()
    base = item.get("base")
    if base is None:
        base = item.get("weight") or 0.0
    base = float(base)
    if base <= 0:
        return 0.0            # deliberately off air; see seasons.off_weight

    s = float((ctx.get("season") or {}).get(item.get("kind"), 1.0))
    d = float((ctx.get("daypart") or {}).get(item.get("kind"), 1.0))
    r = recency(item.get("last_played"), now)
    a = affinity((ctx.get("kind_last") or {}).get(item.get("kind")), now)
    f = fatigue(item.get("play_count"), ctx.get("median_plays", 1))
    return base * s * d * r * a * f


def explain(item, ctx, now=None):
    """Every factor behind a score, for debugging why something did or did not air."""
    now = now or time.time()
    base = float(item.get("base") if item.get("base") is not None else (item.get("weight") or 0))
    s = float((ctx.get("season") or {}).get(item.get("kind"), 1.0))
    d = float((ctx.get("daypart") or {}).get(item.get("kind"), 1.0))
    r = recency(item.get("last_played"), now)
    a = affinity((ctx.get("kind_last") or {}).get(item.get("kind")), now)
    f = fatigue(item.get("play_count"), ctx.get("median_plays", 1))
    return {"base": round(base, 3), "season": round(s, 3), "daypart": round(d, 3),
            "recency": round(r, 3),
            "affinity": round(a, 3), "fatigue": round(f, 3),
            "score": round(base * s * d * r * a * f, 4)}


def build_context(rows, season_factors=None, now=None, daypart_factors=None):
    """Derive the pool-wide state `score()` needs from the rows themselves.

    Kept here rather than in each caller so the player, the random API and the
    fill endpoint all rank material identically — one model, not three that
    drift apart.
    """
    now = now or time.time()
    kind_last, plays = {}, []
    for r in rows:
        k, lp = r.get("kind"), r.get("last_played") or 0
        if lp and lp > kind_last.get(k, 0):
            kind_last[k] = lp
        plays.append(r.get("play_count") or 0)
    plays.sort()
    median = plays[len(plays) // 2] if plays else 0
    return {"kind_last": kind_last, "median_plays": median,
            "season": season_factors or {}, "daypart": daypart_factors or {}, "now": now}


def weights_for(rows, season_factors=None, now=None, daypart_factors=None):
    """Scores for a list of rows, ready to hand to a weighted choice."""
    ctx = build_context(rows, season_factors, now, daypart_factors)
    return [score(r, ctx, ctx["now"]) for r in rows], ctx


def describe_settings():
    """One-line summary of the active curve parameters, for status output and
    debugging "why did that play?" questions against a running config."""
    return ("recency tau=%.1fh floor=%.3f | affinity tau=%.0fmin floor=%.2f | "
            "fatigue strength=%.2f clamp=[%.2f,%.2f]"
            % (RECENCY_TAU / 3600.0, RECENCY_FLOOR, AFFINITY_TAU / 60.0,
               AFFINITY_FLOOR, FATIGUE_STRENGTH, FATIGUE_MIN, FATIGUE_MAX))

