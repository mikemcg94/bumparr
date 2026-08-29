"""Attach a relevant CC/PD background image to text cards, via Openverse.

For each eligible card we derive a search term from its own text, query Openverse
(free CC/public-domain image search), and store the image URL in payload.bg. The
player renders bg with a dark scrim so text stays legible; un-enriched cards stay
on black. No card is ever blocked on an image.
"""
from bumparr import config
import json, sqlite3, time, urllib.request, urllib.parse, re
from bumparr.content_filter import is_grim

DB = config.DB_PATH          # never hardcode a deployment path
API = "https://api.openverse.org/v1/images/"
UA = "bumparr/1.0"
DEADLINE = time.time() + 600

STOP = set("the a an of to in on at is are was were and or but for with from this that "
           "which who what when where why how many much more most than then over under "
           "about into your you our his her its their they them he she it we i as by be "
           "has have had will would can could first only also just each per some".split())

# Kinds worth a background image, with how to derive the query.
ELIGIBLE = {"fun_facts", "number", "on_this_day", "guess_year", "connections"}


def keywords(text, n=3):
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
    if kind == "number":
        return payload.get("meaning", "")
    if kind == "on_this_day":
        lines = payload.get("lines", [])
        return lines[-1] if lines else ""
    lines = payload.get("lines", [])
    return " ".join(lines) if lines else payload.get("text", "")


def search_image(q):
    if not q:
        return None
    url = API + "?" + urllib.parse.urlencode(
        {"q": q, "page_size": 3, "license_type": "all", "mature": "false"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        d = json.load(urllib.request.urlopen(req, timeout=25))
    except Exception as e:
        print("  search err", e)
        return None
    for r in d.get("results", []):
        u = r.get("url") or ""
        if u.startswith("http") and re.search(r"\.(jpg|jpeg|png)(\?|$)", u, re.I):
            return u
    return None


def run():
    c = sqlite3.connect(DB, timeout=15)
    c.execute("PRAGMA busy_timeout=8000")
    rows = c.execute(
        "SELECT id, kind, payload FROM playables WHERE type='card' AND kind IN (%s)"
        % ",".join("'%s'" % k for k in ELIGIBLE)).fetchall()
    print("eligible cards:", len(rows))
    done = 0
    for pid, kind, pjson in rows:
        if time.time() > DEADLINE:
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
        payload["bg"] = img
        payload["bg_q"] = q
        c.execute("UPDATE playables SET payload=? WHERE id=?", (json.dumps(payload), pid))
        c.commit()
        done += 1
        if done <= 12:
            print("  +bg  [%s] q=%r -> %s" % (kind, q, img[:64]))
        time.sleep(1.2)  # polite pacing
    c.close()
    print("enriched with background image:", done)


if __name__ == "__main__":
    run()
