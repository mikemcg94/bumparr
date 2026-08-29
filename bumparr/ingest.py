"""Natural-language "pull this into the rotation" ingest.

Turns a freeform request into new pool content. Three request shapes:
  1. a URL you found  -> add it (live cam / YouTube snapshot / archive item / clip)
  2. "more <type> cards" -> generate that card kind
  3. a vibe/theme ("more stonery stuff") -> search public-domain sources, pull matches

Deterministic where it can be (URL detection, card-kind matching); the local model
only helps turn a fuzzy vibe into search terms + a category, with a plain fallback.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from bumparr import config, db
from bumparr.card_validation import validate_card

UA = {"User-Agent": "Mozilla/5.0 bumparr (polite)"}
ASSET = config.ASSET_ROOT

CARD_KINDS = {"trivia", "fun_facts", "psa", "on_this_day", "number", "corrections",
              "achievements", "coming_up", "technical_difficulties",
              "station_id", "dead_air"}

# Requests that should pull still IMAGES from the Library of Congress (PD by statute).
LOC_HINTS = ("poster", "propaganda", "uncle sam", "war bond", "war bonds", "declassified",
             "wpa", "recruitment", "vintage photo", "historical photo", "old photo",
             "vintage poster", "government poster", "loc ", "library of congress")

# vibe word -> archive.org search terms + target category
VIBE_MAP = {
    "stoner": ("(psychedelic OR experimental OR abstract OR kaleidoscope OR fractal)", "trippy"),
    "trippy": ("(psychedelic OR experimental OR abstract OR kaleidoscope)", "trippy"),
    "psychedelic": ("(psychedelic OR experimental OR light show)", "trippy"),
    "space": ("(space OR nebula OR galaxy OR astronomy)", "ambient"),
    "cartoon": ("(cartoon OR animation OR betty boop OR popeye)", "cartoons"),
    "military": ("(navy OR military OR army training)", "military"),
    "atomic": ("(atomic OR nuclear OR civil defense)", "atomic_era"),
    "car": ("(automobile OR racing OR motor)", "automotive"),
    "race": ("(auto racing OR speedway)", "automotive"),
    "nature": ("(nature OR wildlife OR ocean)", "ambient"),
    "ambient": ("(ambient OR calm OR relaxing scenery)", "ambient"),
    "vintage": ("(vintage ephemera OR mid-century)", "vintage_ads"),
    "creepy": ("(strange OR eerie OR unsettling)", "trippy"),
    "science": ("(science OR laboratory OR microscope)", "fun_animation"),
    "sport": ("(sports OR athletics OR game highlights OR olympics)", "sports"),
    "boxing": ("(boxing OR prizefight)", "sports"),
    "baseball": ("(baseball)", "sports"),
    "football": ("(football)", "sports"),
    "storm": ("(storm OR thunderstorm OR lightning OR rain)", "weather"),
    "rain": ("(rain OR rainfall OR storm)", "weather"),
    "snow": ("(snow OR blizzard OR winter)", "weather"),
    "clouds": ("(clouds OR sky OR timelapse)", "weather"),
}


def _gj(url, timeout=30):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout))


def _insert_stream(title, url, direct, kind="webcam", region="user-added"):
    pid = "stream:cam:" + hashlib.md5(url.encode()).hexdigest()[:10]
    payload = json.dumps({"direct": direct, "label": title, "region": region})
    with db.conn() as c:
        if c.execute("SELECT 1 FROM playables WHERE id=?", (pid,)).fetchone():
            return "already in the pool"
        c.execute("INSERT OR IGNORE INTO playables (id,type,kind,source,uri,duration,title,payload,tags,weight,enabled,health,last_played,play_count,created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,1,'ok',0,0,?)",
                  (pid, "stream", kind, "user-added", url, 45.0, title, payload, "live,window,user", 1.2, time.time()))
        c.commit()
    return "added live cam: %s" % title


def _video_aspect(path):
    """Aspect ratio of a video file, or None if unreadable."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60).stdout.strip()
        w, h = [int(x) for x in out.split(",")[:2]]
        return w / float(h) if h else None
    except Exception:
        return None


def _reject_portrait(path):
    """True if this video is phone-format vertical and should not be kept.

    Checked after download because stock APIs report orientation inconsistently.
    Deleting immediately keeps the source tree clean rather than leaving files
    that every later stage has to remember to skip.
    """
    ar = _video_aspect(path)
    if ar is None or ar >= config.MIN_VIDEO_ASPECT:
        return False
    try:
        os.remove(path)
    except Exception:
        pass
    return True


def _download_video(url, category, title=None, reseed=True):
    outdir = os.path.join(ASSET, category)
    os.makedirs(outdir, exist_ok=True)
    fn = title or os.path.basename(urllib.parse.urlparse(url).path) or ("clip_%d.mp4" % int(time.time()))
    if not fn.lower().endswith((".mp4", ".webm", ".mkv")):
        fn += ".mp4"
    dest = os.path.join(outdir, re.sub(r"[^\w.\-]", "_", fn))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180) as r, open(dest, "wb") as out:
            while True:
                b = r.read(262144)
                if not b:
                    break
                out.write(b)
    except Exception as e:
        return "download failed: %s" % e
    if os.path.getsize(dest) < 20000:
        os.remove(dest)
        return "download too small / failed"
    if _reject_portrait(dest):
        return "skipped a vertical phone-format clip (not broadcast-shaped)"
    # Batch pulls pass reseed=False and reseed ONCE at the end — one full-tree
    # reseed per clip hammers the shared DB and triggers 'database is locked'.
    if reseed:
        _reseed()
    return "added clip to %s" % category


def _capture_youtube(url_or_query, category="windows", slug=None):
    """Snapshot a YouTube-live cam as a short looping clip (same path as live windows)."""
    ytdlp = _which("yt-dlp")
    slug = slug or ("yt_" + hashlib.md5(url_or_query.encode()).hexdigest()[:8])
    outdir = os.path.join(ASSET, category)
    os.makedirs(outdir, exist_ok=True)
    dest = os.path.join(outdir, slug + ".mp4")
    try:
        target = url_or_query if url_or_query.startswith("http") else "ytsearch1:" + url_or_query
        u = subprocess.run([ytdlp, "--no-warnings", "--get-url", "-f", "b[height<=720]/b", target],
                           capture_output=True, text=True, timeout=60).stdout.strip().split("\n")[0]
        if not u.startswith("http"):
            return "couldn't resolve that video"
        # Constant frame rate, not -c copy: some cam encoders declare a nominal
        # rate they do not actually deliver, and copying preserves the resulting
        # stutter. Same reason as sources/capture_windows.py.
        subprocess.run(["ffmpeg", "-y", "-rw_timeout", "15000000", "-i", u, "-t", "35",
                        "-vsync", "cfr", "-r", "30", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                        "-an", "-f", "mp4", dest],
                       capture_output=True, text=True, timeout=240)
    except Exception as e:
        return "capture failed: %s" % e
    if not os.path.exists(dest) or os.path.getsize(dest) < 50000:
        return "capture failed"
    _reseed()
    return "captured a snippet into %s" % category


def _which(name):
    from shutil import which
    return which(name) or name


def _reseed():
    from bumparr import seed
    seed.seed_from_assets()


def _fetch_archive(identifier, category, reseed=True):
    try:
        m = _gj("https://archive.org/metadata/%s" % identifier)
    except Exception as e:
        return "archive lookup failed: %s" % e
    files = m.get("files", [])
    mp4 = None
    for f in sorted(files, key=lambda f: (0 if f.get("name", "").endswith("_512kb.mp4") else 1)):
        if f.get("name", "").lower().endswith(".mp4") and int(f.get("size", 0) or 0) < 150 * 1048576:
            mp4 = f["name"]; break
    if not mp4:
        return "no usable video in that archive item"
    url = "https://archive.org/download/%s/%s" % (identifier, urllib.parse.quote(mp4))
    return _download_video(url, category, title="%s__%s" % (identifier, mp4), reseed=reseed)


def _download_image(url, category, stem, title=""):
    """Download a still image and register it directly as a type=image playable
    (reseed only scans videos, so images must be inserted here)."""
    outdir = os.path.join(ASSET, category)
    os.makedirs(outdir, exist_ok=True)
    dest = os.path.join(outdir, stem + ".jpg")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r, open(dest, "wb") as out:
            out.write(r.read())
    except Exception:
        return False
    if os.path.getsize(dest) < 8000:
        os.remove(dest)
        return False
    rel = "%s/%s.jpg" % (category, stem)
    pid = "img:" + rel
    payload = json.dumps({"pan": True, "title": (title or "")[:120], "source": "Library of Congress"})
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO playables (id,type,kind,source,uri,duration,title,payload,tags,weight,enabled,health,last_played,play_count,created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,1,'ok',0,0,?)",
                  (pid, "image", category, "loc", rel, 9.0, (title or "Library of Congress")[:80],
                   payload, "image,pd,gov,loc", 1.0, time.time()))
        c.commit()
    return True


def _loc_search(keywords, category, want=3):
    """Library of Congress — federal / historical PD images (posters, propaganda,
    photos). Keyless JSON API. Federal works are public domain by statute."""
    q = " ".join(keywords[:4])
    url = "https://www.loc.gov/photos/?q=%s&fo=json&c=40" % urllib.parse.quote(q)
    try:
        results = _gj(url).get("results", [])
    except Exception:
        return []
    pulled = []
    for r in results:
        if len(pulled) >= want:
            break
        iu = r.get("image_url") or []
        if not iu:
            continue
        item_id = (r.get("id", "").rstrip("/").split("/")[-1]) or hashlib.md5((r.get("title") or "").encode()).hexdigest()[:8]
        stem = "loc_" + re.sub(r"[^\w]", "", item_id)[:24]
        if _already_have(category, stem):
            continue
        img_url = iu[-1].split("#")[0]  # biggest size; strip the #h=..&w=.. fragment
        if not img_url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".gif")):
            continue
        if _download_image(img_url, category, stem, title=r.get("title", "")):
            pulled.append(item_id)
        time.sleep(1)
    return pulled


def _already_have(category, stem):
    """True if a file for this item is already in the category — so repeated pulls
    skip what's there and keep expanding variety instead of overlapping."""
    d = os.path.join(ASSET, category)
    if not os.path.isdir(d):
        return False
    stem = re.sub(r"[^\w.\-]", "_", stem)
    return any(f.startswith(stem) for f in os.listdir(d))


def _pexels_search(keywords, category, want=2):
    """Pexels video API — clean modern b-roll, free license, no attribution required."""
    key = config.PEXELS_API_KEY
    if not key:
        return []
    q = " ".join(keywords[:4])
    # Fetch a big candidate pool so we can still find `want` NEW ones after skipping
    # everything already pulled into this category.
    req = urllib.request.Request(
        "https://api.pexels.com/videos/search?per_page=50&query=" + urllib.parse.quote(q),
        headers={"Authorization": key, **UA})
    try:
        vids = json.load(urllib.request.urlopen(req, timeout=25)).get("videos", [])
    except Exception:
        return []
    pulled = []
    for v in vids:
        if len(pulled) >= want:
            break
        if _already_have(category, "pexels_%s" % v.get("id")):
            continue
        # pick a mid-size mp4 (prefer ~hd, <=1280 wide to keep files sane)
        files = sorted([f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4"],
                       key=lambda f: abs((f.get("width") or 0) - 1280))
        if not files:
            continue
        url = files[0]["link"]
        r = _download_video(url, category, title="pexels_%s" % v.get("id"), reseed=False)
        if r.startswith("added"):
            pulled.append("pexels#%s" % v.get("id"))
        time.sleep(1)
    if pulled:
        _reseed()
    return pulled


def _pixabay_search(keywords, category, want=2):
    """Pixabay video API — free stock footage, Pixabay Content License (no attribution)."""
    key = config.PIXABAY_API_KEY
    if not key:
        return []
    q = " ".join(keywords[:4])
    url = ("https://pixabay.com/api/videos/?key=%s&per_page=50&q=%s"
           % (key, urllib.parse.quote(q)))
    try:
        hits = _gj(url).get("hits", [])
    except Exception:
        return []
    pulled = []
    for h in hits:
        if len(pulled) >= want:
            break
        if _already_have(category, "pixabay_%s" % h.get("id")):
            continue
        vids = h.get("videos", {})
        pick = vids.get("medium") or vids.get("small") or vids.get("large") or vids.get("tiny")
        if not pick or not pick.get("url"):
            continue
        r = _download_video(pick["url"], category, title="pixabay_%s" % h.get("id"), reseed=False)
        if r.startswith("added"):
            pulled.append("pixabay#%s" % h.get("id"))
        time.sleep(1)
    if pulled:
        _reseed()
    return pulled


def _wikimedia_search(keywords, category, want=2):
    """Search Wikimedia Commons for video — everything there is CC/PD by policy, so
    it's a clean second source that catches things archive.org misses."""
    q = " ".join(keywords[:4]) + " filetype:video"
    api = ("https://commons.wikimedia.org/w/api.php?action=query&list=search&"
           "srsearch=%s&srnamespace=6&format=json&srlimit=40" % urllib.parse.quote(q))
    try:
        hits = _gj(api).get("query", {}).get("search", [])
    except Exception:
        return []
    kwset = set(keywords)
    pulled = []
    for h in hits:
        if len(pulled) >= want:
            break
        title = h["title"]  # e.g. "File:President Clinton Golfing.webm"
        low = title.lower()
        if kwset and not any(k in low for k in kwset):
            continue
        if _already_have(category, "wm_" + title.replace("File:", "")):
            continue
        try:
            info = _gj("https://commons.wikimedia.org/w/api.php?action=query&titles=%s"
                       "&prop=imageinfo&iiprop=url|size|mediatype&format=json"
                       % urllib.parse.quote(title))
            page = next(iter(info["query"]["pages"].values()))
            ii = page["imageinfo"][0]
        except Exception:
            continue
        if ii.get("mediatype") != "VIDEO":
            continue
        if int(ii.get("size", 0) or 0) > 150 * 1048576:
            continue
        url = ii["url"]
        fn = "wm_" + re.sub(r"[^\w.\-]", "_", title.replace("File:", ""))
        r = _download_video(url, category, title=fn, reseed=False)
        if r.startswith("added"):
            pulled.append(title.replace("File:", ""))
        time.sleep(2)
    if pulled:
        _reseed()
    return pulled


def _test_cors(m3u8):
    try:
        r = urllib.request.urlopen(urllib.request.Request(m3u8, headers=UA), timeout=12)
        return r.headers.get("Access-Control-Allow-Origin") == "*"
    except Exception:
        return False


# ---------- intent parsing ----------

URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def _model_intent(text):
    """Optional: ask the local model to turn a vibe into {terms, category}. Falls back."""
    base = config.LOCAL_LLM_BASE
    if not base:
        return None
    prompt = ("Turn this TV-bumper content request into JSON with keys "
              "\"search_terms\" (3-6 words for an archive.org search) and "
              "\"category\" (one of: trippy, ambient, cartoons, military, atomic_era, "
              "automotive, fun_animation, vintage_ads). Request: %r. "
              "Return ONLY the JSON object." % text)
    try:
        body = json.dumps({"model": config.LOCAL_LLM_MODEL,
                           "messages": [{"role": "user", "content": prompt}],
                           "temperature": 0.3, "max_tokens": 200,
                           "chat_template_kwargs": {"enable_thinking": False}}).encode()
        req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(req, timeout=60))
        content = d["choices"][0]["message"].get("content") or ""
        mt = re.search(r"\{.*\}", content, re.DOTALL)
        if mt:
            obj = json.loads(mt.group(0))
            if obj.get("search_terms"):
                return obj
    except Exception:
        pass
    return None


# archive.org collections that are reliably public-domain / openly-licensed, so an
# item in one is safe even when its licenseurl field is blank (common for old/gov films).
OPEN_COLLECTIONS = {
    "prelinger", "prelingerhomemovies", "animationandcartoons", "classic_tv",
    "classic_cartoons", "film_noir", "scifi_horror", "more_animation",
    "opensource_movies", "feature_films", "computerimagesystems", "gratefuldead",
    "nasa", "sabucat", "publicmoviescollection", "silenthalloffame",
}


def _license_ok(doc):
    lic = (doc.get("licenseurl") or "").lower()
    if "publicdomain" in lic or "creativecommons" in lic:
        return True
    cols = doc.get("collection") or []
    if isinstance(cols, str):
        cols = [cols]
    return any(c.lower() in OPEN_COLLECTIONS for c in cols)


STOP = set("a an the of to in on at is are was were and or but for with from this that "
           "some more few new old i want give me find get pull add show footage clips clip "
           "video videos stuff things thing please can you my our".split())


def _keywords(text):
    words = re.findall(r"[a-zA-Z][a-zA-Z'-]{2,}", text.lower())
    kw = [w for w in words if w not in STOP]
    return kw or words


def _category_for(text, keywords):
    """Map to a known category if the request clearly matches one; otherwise make a
    clean new category named after the request (better than forcing a bad fit)."""
    low = text.lower()
    for word, (_, cat) in VIBE_MAP.items():
        if word in low:
            return cat
    slug = "_".join(keywords[:2]) if keywords else "misc"
    return re.sub(r"[^a-z0-9_]", "", slug)[:24] or "misc"


VINTAGE_HINTS = ("vintage", "old", "retro", "classic", "1920", "1930", "1940", "1950",
                 "1960", "black and white", "black-and-white", "b&w", "antique",
                 "cartoon", "silent film", "newsreel", "archival", "historic")


DEFAULT_COUNT = 3
MAX_COUNT = 15


def _extract_count(text):
    """Pull a requested clip count from the text ('5 golf clips', 'pull 8 rain').
    Returns None if unspecified. Capped at MAX_COUNT to stay polite to sources/disk."""
    m = re.search(r"\b(\d{1,2})\b", text)
    if not m:
        return None
    return max(1, min(MAX_COUNT, int(m.group(1))))


def _theme_search(text, count=DEFAULT_COUNT):
    """Vibe request -> pull N clips from the best source for the intent.
    Modern filler (e.g. 'golf') hits stock footage first; vintage requests hit the
    film archives first. Sources: Pexels/Pixabay (stock), archive.org, Wikimedia."""
    kw = _keywords(text)
    category = _category_for(text, kw)
    low_all = text.lower()
    vintage = any(h in low_all for h in VINTAGE_HINTS)

    # Stock footage is ideal for generic/modern filler but pointless for vintage asks.
    if not vintage:
        for src, name in ((_pexels_search, "Pexels"), (_pixabay_search, "Pixabay")):
            hits = src(kw, category, want=count)
            if hits:
                return "pulled %d stock clip(s) into '%s' (%s): %s" % (len(hits), category, name, ", ".join(hits))

    # Prefer the built-in vibe terms when the request matches one; else AND the
    # request's own keywords (deterministic — no model drift), relevance-sorted.
    terms = None
    low = text.lower()
    for word, (t, cat) in VIBE_MAP.items():
        if word in low:
            terms = t
            break
    if not terms:
        terms = " AND ".join(kw[:4]) if kw else re.sub(r"[^\w ]", "", text)[:60]
    q = "(%s) AND mediatype:movies" % terms
    try:
        # NO sort -> archive.org relevance ranking (downloads-sort surfaces unrelated popular items)
        s = _gj("https://archive.org/advancedsearch.php?q=%s&fl[]=identifier&fl[]=title&fl[]=licenseurl&fl[]=collection&rows=25&output=json"
                % urllib.parse.quote(q))
        docs = s.get("response", {}).get("docs", [])
    except Exception as e:
        return "search failed: %s" % e
    if not docs:
        return "no archive.org matches for that — try different words or paste a URL"
    # Relevance guard: the title should contain at least one of the keywords.
    kwset = set(kw)
    def relevant(d):
        t = (d.get("title") or "").lower()
        return not kwset or any(k in t for k in kwset)
    pulled, skipped_license, skipped_rel = [], 0, 0
    for d in docs:
        if len(pulled) >= count:
            break
        if not relevant(d):
            skipped_rel += 1
            continue
        if _already_have(category, d["identifier"]):
            continue
        if not _license_ok(d):
            skipped_license += 1
            continue
        r = _fetch_archive(d["identifier"], category, reseed=False)
        if r.startswith("added"):
            pulled.append(d["identifier"])
        time.sleep(3)
    if pulled:
        _reseed()
        return "pulled %d clip(s) into '%s': %s" % (len(pulled), category, ", ".join(pulled))
    # archive.org came up empty/blocked -> try Wikimedia Commons (all free-licensed).
    wm = _wikimedia_search(kw, category, want=count)
    if wm:
        return "pulled %d clip(s) into '%s' (Wikimedia): %s" % (len(wm), category, ", ".join(wm))
    if skipped_license:
        return ("found %d archive matches but they weren't clearly free to use, and Wikimedia "
                "had nothing — paste a specific link if you know it's PD." % skipped_license)
    return "nothing clean found for that on archive.org or Wikimedia — try different words or a URL"


def handle(text):
    """Main entry: route a freeform request to the right handler. Returns a message."""
    text = (text or "").strip()
    if not text:
        return "type something to add"

    # 1) URL present?
    m = URL_RE.search(text)
    if m:
        url = m.group(0)
        low = url.lower()
        if ".m3u8" in low:
            direct = _test_cors(url)
            return _insert_stream("Live: user cam", url, direct)
        if "youtube.com" in low or "youtu.be" in low:
            return _capture_youtube(url)
        mi = re.search(r"archive\.org/(?:details|download)/([^/?#]+)", low)
        if mi:
            cat = "trippy"
            for word, (_, c) in VIBE_MAP.items():
                if word in text.lower():
                    cat = c; break
            return _fetch_archive(mi.group(1), cat)
        if low.split("?")[0].endswith((".mp4", ".webm", ".mkv")):
            return _download_video(url, "windows")
        return "found a URL but not sure how to use it (want .m3u8, youtube, archive.org, or a direct video)"

    low = text.lower()

    # 2a) weather DATA request -> live weather card (distinct from weather footage).
    #     "weather", "weather in Tokyo", "weather at my destination / home".
    wm = re.search(r"weather(?:\s+(?:in|for|at))?\s+(.+)$", low)
    if "weather" in low and ("in " in low or "for " in low or " at " in low or low.strip() in ("weather", "current weather")):
        place = None
        if wm:
            place = wm.group(1).strip()
            if place in ("home", "my location", "here", "current location", "my destination", "destination"):
                place = None
        return _weather_card(place)

    # 2a-2) Library of Congress image request (posters / propaganda / declassified /
    #       historical photos). Federal works are PD by statute.
    if any(h in low for h in LOC_HINTS):
        count = _extract_count(text) or DEFAULT_COUNT
        kw = _keywords(text)
        cat = ("posters" if any(w in low for w in ("poster", "propaganda", "uncle sam", "war bond"))
               else "gov_photos" if ("declassified" in low or "photo" in low) else _category_for(text, kw))
        hits = _loc_search(kw, cat, want=count)
        if hits:
            return "pulled %d PD image(s) into '%s' (Library of Congress): %s" % (len(hits), cat, ", ".join(hits))
        return "Library of Congress had nothing usable for that — try different words"

    # 2b) "more <card kind>"
    for kind in CARD_KINDS:
        if kind.replace("_", " ") in low or kind in low:
            return _generate_cards(kind)
    if "tech" in low and "diff" in low or "dead air" in low or "stand by" in low:
        return _generate_cards("technical_difficulties")
    if "trivia" in low:
        return _generate_cards("trivia")

    # 3) vibe/theme search — honor a count in the request ("5 golf clips")
    count = _extract_count(text) or DEFAULT_COUNT
    return _theme_search(text, count)


# Procedural visual cards (rendered by the player, registered directly — not model-generated).
PROCEDURAL = {
    "technical_difficulties": [
        ("PLEASE STAND BY", "bars"), ("WE'LL BE RIGHT BACK", "bars"),
        ("DO NOT ADJUST YOUR SET", "static"), ("ONE MOMENT PLEASE", "bars"),
        ("TRANSMISSION INTERRUPTED", "nosignal"), ("SIGNAL LOST", "static"),
        ("NORMAL SERVICE WILL RESUME SHORTLY", "bars"), ("STAND BY", "nosignal"),
        ("{BRAND} WILL RETURN", "bars"), ("PARDON OUR INTERRUPTION", "bars"),
        ("BACK AFTER THIS", "bars"), ("DO NOT ATTEMPT TO ADJUST THE PICTURE", "static"),
    ],
    "dead_air": [("", "")],
}


def _register_procedural(kind):
    items = PROCEDURAL.get(kind)
    if not items:
        return "no procedural set for %s" % kind
    added = 0
    with db.conn() as c:
        for text, variant in items:
            payload = json.dumps({"variant": variant, "text": text} if variant else {})
            pid = "card:%s:%s" % (kind, hashlib.md5((kind + text + variant).encode()).hexdigest()[:10])
            before = c.total_changes
            c.execute("INSERT OR IGNORE INTO playables (id,type,kind,source,uri,duration,title,payload,tags,weight,enabled,health,last_played,play_count,created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,1,'ok',0,0,?)",
                      (pid, "card", kind, "render", None, 8.0, (text or kind)[:40], payload, "visual,user", 0.6, time.time()))
            added += c.total_changes - before
        c.commit()
    return "added %d %s card(s)" % (added, kind.replace("_", " "))


# Model-free starter cards for the otherwise model-generated kinds, so a
# no-model install is never empty. A model diversifies on top; these are the
# floor. See config_files/card_seeds.json (plain, replaceable examples).
MODEL_CARD_KINDS = {"psa", "corrections", "coming_up", "achievements", "tiny_games"}
_CARD_SEEDS_FILE = os.path.join(os.path.dirname(__file__), "config_files", "card_seeds.json")
_CARD_SEEDS = None


def _load_card_seeds():
    global _CARD_SEEDS
    if _CARD_SEEDS is None:
        try:
            _CARD_SEEDS = json.load(open(_CARD_SEEDS_FILE, encoding="utf-8"))
        except Exception as e:
            print("[bumparr] card seeds unavailable: %s" % e)
            _CARD_SEEDS = {}
    return _CARD_SEEDS


def register_card_seeds(kind):
    """Insert the shipped model-free starter cards for one kind (idempotent)."""
    seeds = _load_card_seeds().get(kind, [])
    available = 0
    with db.conn() as c:
        for obj in seeds:
            clean, _r = validate_card(kind, obj)
            if clean is None:
                continue
            available += 1
            payload = {"lines": clean["lines"]}
            if kind == "tiny_games":
                payload["answer"] = clean.get("answer", "")
            pj = json.dumps(payload, sort_keys=True)
            pid = "card:%s:seed:%s" % (kind, hashlib.md5(pj.encode()).hexdigest()[:10])
            c.execute("INSERT OR IGNORE INTO playables (id,type,kind,source,uri,duration,title,payload,tags,weight,enabled,health,last_played,play_count,created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,1,'ok',0,0,?)",
                      (pid, "card", kind, "seed", None, config.CARD_DEFAULT_DURATION,
                       str(clean["lines"][0])[:80], pj, "starter", 0.7, time.time()))
        c.commit()
    return available


def register_all_baselines():
    """Register every model-free card floor (procedural + starter seeds).
    Safe and idempotent; run on startup so a fresh, model-less install has
    content in every kind."""
    for k in PROCEDURAL:
        _register_procedural(k)
    total = sum(register_card_seeds(k) for k in MODEL_CARD_KINDS)
    return total


def _weather_card(place):
    """Live weather card via Open-Meteo. place=None uses the configured home."""
    location = place or config.env("HOME_LOCATION", "")
    try:
        from bumparr.generators import weather
        return weather.generate(location)
    except Exception as e:
        return "weather lookup failed: %s" % e


def _generate_cards(kind):
    if kind in PROCEDURAL:
        return _register_procedural(kind)
    grounded = {"trivia", "fun_facts", "number"}
    try:
        if kind in grounded:
            r = subprocess.run([sys.executable, "-m", "bumparr.generators.grounded", "--kind", kind, "--n", "15"],
                               capture_output=True, text=True, timeout=300)
        elif kind == "on_this_day":
            r = subprocess.run([sys.executable, "-m", "bumparr.generators.on_this_day", "--n", "15"],
                               capture_output=True, text=True, timeout=120)
        else:
            # Model-invented kind: register the model-free baseline first, then
            # let a model diversify. With no model, the baseline is the answer.
            base = register_card_seeds(kind)
            if not config.LOCAL_LLM_BASE:
                return ("%s: no model configured; %d built-in starter card(s) available. "
                        "Set your model endpoint to generate more in your own voice."
                        % (kind.replace("_", " "), base))
            r = subprocess.run([sys.executable, "-m", "bumparr.generators.cards", "--kind", kind, "--n", "15"],
                               capture_output=True, text=True, timeout=300)
        out = (r.stdout or r.stderr).strip().split("\n")[-1]
        return "generated more %s cards — %s" % (kind, out)
    except Exception as e:
        return "generation failed: %s" % e
