"""On This Day cards from Wikipedia's free feed API (no key).

Wikipedia's events feed skews heavily grim (wars, bombings, disasters), so every
event is passed through the shared tone filter. Only lighter events become cards.

These cards are DATE-BOUND, which makes them perishable in a way the clock and
weather cards are not: re-rendering does not help, because the payload itself
names an event tied to one calendar day. A card generated on the 17th is simply
wrong on the 19th.

They are not thrown away, though. An August 17 card is correct every August 17,
so cards are stamped with the day they belong to and only enabled while that day
is today. The pool therefore accumulates a calendar that rotates itself in.

Usage:  python -m bumparr.generators.on_this_day --n 20
"""
import argparse
import hashlib
import json
import time
import urllib.request

from bumparr import config, db
from bumparr.content_filter import weight_for


def fetch_events():
    """Today's events from Wikipedia's on-this-day feed (list of dicts with
    year/text), or [] if the feed is unreachable."""
    lt = time.localtime()
    url = ("https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/%02d/%02d"
           % (lt.tm_mon, lt.tm_mday))
    req = urllib.request.Request(url, headers={"User-Agent": "bumparr/1.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r).get("events", [])


def today_key():
    """Today as MM-DD — the key stamped into each card's payload (for_date)
    and matched against by is_todays_card."""
    lt = time.localtime()
    return "%02d-%02d" % (lt.tm_mon, lt.tm_mday)


def is_todays_card(payload, today=None):
    """Does this on_this_day payload belong to `today` (MM-DD)?

    The one place that decides, because two callers ask the same question for
    opposite reasons: the rotation parks every card this says no to, and the
    /api/pool/enable warning tells the operator which answer their card got.
    They used to decide separately — the rotation with SQL
    `LIKE '%"for_date": "MM-DD"%'` against the serialized text, the warning by
    parsing the JSON — so any payload shape json.dumps' defaults do not produce
    made them disagree: a compact-serialized card (no space after the colon)
    was told it "stays on" and parked on the next pass, and a NULL payload was
    promised a park that never came (`payload NOT LIKE ?` is NULL, not true,
    for a NULL payload).

    Anything that is not a JSON object stamped with today's `for_date` is not
    today's card: a NULL, malformed or non-dict payload answers False rather
    than raising, which is what parks it.
    """
    today = today or today_key()
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return False
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "null")
        except ValueError:
            return False
    if not isinstance(payload, dict):
        return False
    return payload.get("for_date") == today


def retire_other_days(c, today=None):
    """Enable only the cards belonging to today; park the rest.

    Disabled rather than deleted, because these come back around: the same card
    is correct again next year on its own date, and regenerating it would just
    re-fetch identical text from Wikipedia.

    Matching is Python-side (is_todays_card), so how the payload was serialized
    cannot change the answer. Returns (on, off) as before: how many parked cards
    were switched on, and how many live ones were parked. Rows already in the
    right state are counted in neither, so `0, 0` still means "nothing moved".
    """
    today = today or today_key()
    rows = c.execute(
        "SELECT id, payload, enabled FROM playables WHERE kind='on_this_day'").fetchall()
    on = [r["id"] for r in rows
          if not r["enabled"] and is_todays_card(r["payload"], today)]
    off = [r["id"] for r in rows
           if r["enabled"] and not is_todays_card(r["payload"], today)]
    if on:
        c.executemany("UPDATE playables SET enabled=1 WHERE id=?", [(i,) for i in on])
    if off:
        c.executemany("UPDATE playables SET enabled=0 WHERE id=?", [(i,) for i in off])
    return len(on), len(off)


def generate(target: int) -> int:
    """Top up today's on-this-day cards to `target`, then rotate the calendar.

    Grim events pass the shared tone filter (kept but rare). After inserting,
    calls retire_other_days so only today's cards are enabled — the pool
    accumulates a year-round calendar that switches itself over at midnight
    (see the module docstring for why the cards are parked, not deleted).
    Returns the number of new cards added today.
    """
    events = fetch_events()
    today = today_key()
    added = 0
    with db.conn() as c:
        # Count only TODAY's cards toward the target; yesterday's are parked, not
        # competing for the quota, or a full pool would block today's batch. The
        # same test the rotation uses, so the quota cannot count a card as
        # today's that retire_other_days is about to park (or the reverse).
        have = sum(1 for r in c.execute(
            "SELECT payload FROM playables WHERE kind='on_this_day'").fetchall()
            if is_todays_card(r["payload"], today))
        for ev in events:
            if have + added >= target:
                break
            yr = ev.get("year")
            tx = (ev.get("text") or "").strip()
            if not yr or not tx:
                continue
            # Grim events are kept but rare (heavily down-weighted), never dropped.
            weight = weight_for(0.7, tx)
            lines = ["ON THIS DAY", str(yr), tx]
            pj = json.dumps({"lines": lines, "for_date": today}, sort_keys=True)
            pid = "card:on_this_day:" + hashlib.md5(pj.encode()).hexdigest()[:12]
            before = c.total_changes
            c.execute(
                """INSERT OR IGNORE INTO playables
                   (id,type,kind,source,uri,duration,title,payload,tags,weight,enabled,health,last_played,play_count,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,1,'ok',0,0,?)""",
                (pid, "card", "on_this_day", "generated", None,
                 config.CARD_DEFAULT_DURATION, tx[:80], pj, "", weight, time.time()),
            )
            if c.total_changes > before:
                added += 1
        on, off = retire_other_days(c, today)
        c.commit()
    if on or off:
        print("[on_this_day] %s: %d card(s) brought back, %d parked" % (today, on, off))
    return added


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    args = ap.parse_args()
    db.init_db()
    print("added clean on_this_day cards:", generate(args.n))
