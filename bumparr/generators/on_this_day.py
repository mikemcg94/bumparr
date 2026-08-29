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
    lt = time.localtime()
    url = ("https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/%02d/%02d"
           % (lt.tm_mon, lt.tm_mday))
    req = urllib.request.Request(url, headers={"User-Agent": "bumparr/1.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r).get("events", [])


def _today_key():
    lt = time.localtime()
    return "%02d-%02d" % (lt.tm_mon, lt.tm_mday)


def retire_other_days(c, today=None):
    """Enable only the cards belonging to today; park the rest.

    Disabled rather than deleted, because these come back around: the same card
    is correct again next year on its own date, and regenerating it would just
    re-fetch identical text from Wikipedia.
    """
    today = today or _today_key()
    on = c.execute(
        "UPDATE playables SET enabled=1 WHERE kind='on_this_day' "
        "AND payload LIKE ? AND enabled=0", ('%"for_date": "' + today + '"%',)).rowcount
    off = c.execute(
        "UPDATE playables SET enabled=0 WHERE kind='on_this_day' "
        "AND enabled=1 AND payload NOT LIKE ?", ('%"for_date": "' + today + '"%',)).rowcount
    return on, off


def generate(target: int) -> int:
    events = fetch_events()
    today = _today_key()
    added = 0
    with db.conn() as c:
        # Count only TODAY's cards toward the target; yesterday's are parked, not
        # competing for the quota, or a full pool would block today's batch.
        have = c.execute(
            "SELECT COUNT(*) FROM playables WHERE kind='on_this_day' AND payload LIKE ?",
            ('%"for_date": "' + today + '"%',)).fetchone()[0]
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
