"""Attach a relevant CC0/public-domain background image via Openverse.

For each eligible card we derive a search term from its own text, query Openverse
(free CC/public-domain image search), and store the image URL in payload.bg. The
player renders bg with a dark scrim so text stays legible; un-enriched cards stay
on black. No card is ever blocked on an image.
"""
import json
import re
import time
import urllib.parse
import urllib.request

from bumparr import db
from bumparr.content_filter import is_grim

API = "https://api.openverse.org/v1/images/"
UA = "bumparr/1.0"

STOP = set("the a an of to in on at is are was were and or but for with from this that "
           "which who what when where why how many much more most than then over under "
           "about into your you our his her its their they them he she it we i as by be "
           "has have had will would can could first only also just each per some".split())

# Kinds worth a background image, with how to derive the query.
ELIGIBLE = {"fun_facts", "number", "on_this_day", "guess_year", "connections"}


def keywords(text, n=3):
    """The best image-search terms in a card's text, proper nouns first.

    "The 1909 Hudson-Fulton Celebration" should pull pictures of the
    celebration, not of "the" and "celebration" in general — so capitalised
    words win, and stopwords are dropped wherever they appear.
    """
    words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text)
    # Prefer Proper Nouns (capitalised, not sentence-start-only) for a sharper image.
    proper = [w for w in words if w[0].isupper() and w.lower() not in STOP]
    picks = []
    for w in proper + words:
        lw = w.lower()
        if lw in STOP or lw in [p.lower() for p in picks]:
            continue
        picks.append(w)
        if len(picks) >= n:
            break
    return " ".join(picks)


def card_text(kind, payload):
    """The part of a card that a background image would illustrate.

    Per-kind because the searchable subject isn't always the whole payload: a
    number card's subject is its MEANING, an on-this-day card's is the event,
    not the "ON THIS DAY" / year scaffolding around it.
    """
    if kind == "number":
        return payload.get("meaning", "")
    if kind == "on_this_day":
        lines = payload.get("lines", [])
        return lines[-1] if lines else ""
    lines = payload.get("lines", [])
    return " ".join(lines) if lines else payload.get("text", "")


def search_image(q):
    """CC0/public-domain image metadata from Openverse for `q`, or None.

    None (never an exception) is the contract: a card without a background is
    still a complete card, so enrichment must be able to give up silently.
    """
    if not q:
        return None
    url = API + "?" + urllib.parse.urlencode(
        {"q": q, "page_size": 3, "license": "cc0,pdm", "mature": "false"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as response:
            d = json.load(response)
    except Exception as e:
        print("  search err", e)
        return None
    for r in d.get("results", []):
        u = r.get("url") or ""
        if u.startswith("http") and re.search(r"\.(jpg|jpeg|png)(\?|$)", u, re.IGNORECASE):
            return {"url": u, "creator": r.get("creator") or "",
                    "title": r.get("title") or "",
                    "license": r.get("license") or "",
                    "license_url": r.get("license_url") or "",
                    "source_page": r.get("foreign_landing_url") or ""}
    return None


def run():
    """Attach background images to every eligible card that lacks one.

    Idempotent (payload.bg present = skip), grim-text aware (never a violent
    photo behind a bumper), and deadline-bounded so a scheduled run cannot
    hold the shared DB forever.
    """
    deadline = time.time() + 600
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, kind, payload FROM playables WHERE type='card' AND kind IN (%s)"
            % ",".join("'%s'" % k for k in ELIGIBLE)).fetchall()
    print("eligible cards:", len(rows))
    done = 0
    for pid, kind, pjson in rows:
        if time.time() > deadline:
            print("deadline hit"); break
        try:
            payload = json.loads(pjson or "{}")
        except Exception:
            continue
        if payload.get("bg"):
            continue  # already enriched
        text = card_text(kind, payload)
        if is_grim(text):
            continue  # never put a violence/disaster image behind a bumper
        q = keywords(text)
        if is_grim(q):
            continue
        img = search_image(q)
        if not img:
            continue
        payload["bg"] = img["url"]
        payload["bg_q"] = q
        payload["bg_creator"] = img["creator"]
        payload["bg_title"] = img["title"]
        payload["bg_license"] = img["license"]
        payload["bg_license_url"] = img["license_url"]
        payload["bg_source_page"] = img["source_page"]
        # Keep network waits outside SQLite transactions: enrichment runs while
        # the app is live, and holding a write lock across polite sleeps would
        # block playback history and every other generator.
        with db.conn() as c:
            changed = c.execute(
                "UPDATE playables SET payload=? WHERE id=? AND payload=?",
                (json.dumps(payload), pid, pjson),
            ).rowcount
        if not changed:
            continue  # another writer refreshed the card; retry next pass
        done += 1
        if done <= 12:
            print("  +bg  [%s] q=%r -> %s" % (kind, q, img["url"][:64]))
        time.sleep(1.2)  # polite pacing
    print("enriched with background image:", done)


if __name__ == "__main__":
    run()
