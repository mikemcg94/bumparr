"""The brand slam: a slot-machine font roulette that lands on the brand mark.

This is the signature. In the reference web player it is a live DOM animation
that re-rolls on every play, which a file cannot do — an MP4 is the same bytes
every time. So the randomness moves from per-PLAY to per-CLIP: each rendered
bumper rolls its own landing font once, at production time, and variety comes
from the diversity of the pool rather than from dice at playback. With hundreds
of clips in rotation a viewer sees a different roll constantly; only replaying
the identical clip repeats one, which cooldowns already make rare.

That trade is also the open-core seam. Bumparr bakes a roll into the artifact so
every consumer gets the signature; the licensed player keeps the live per-play
roulette on top, which is a real thing to sell rather than a consolation.

Timing mirrors the player: about 1.5s of flicker decelerating from ~55ms to
~245ms between swaps, then a hold on the landing font.
"""
import os
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from bumparr import config

FLICKER_TOTAL = 1.5         # seconds of rolling before it lands, when there is room
INTERVAL_START = 0.035      # fastest swap — the blur at the start of the spin
INTERVAL_END = 0.235        # slowest swap, just before landing
MAX_FACES = 22              # how many typefaces one roll may draw from

# How long the mark is on screen. Proportional to the clip so a long bumper gets
# a proper sign-off, with a hard floor so a short one is still readable — the
# viewer has to have time to NOTICE it, which a fixed slice cannot guarantee.
SLAM_FRACTION = 0.25
SLAM_MIN = 2.8              # floor: enough for a roll plus a readable hold
SLAM_MAX = 6.0              # ceiling: a sign-off, not a second feature
SLAM_MAX_SHARE = 0.60       # never let the mark eat most of a very short clip
HOLD_MIN = 1.2              # the landed mark must sit still this long
FLICKER_MIN = 0.45          # below this a "roll" is just a flicker artefact

FONT_EXT = (".ttf", ".otf")


def font_pool(limit=None):
    """Every usable typeface the deployer has mounted.

    Bumparr bundles two OFL faces so a bare install renders text at all, but the
    roulette wants a crowd — the user's own `/fonts` mount is what makes it look
    like a slot machine instead of a toggle.
    """
    seen, out = set(), []
    for d in (config.FONT_DIR, Path(__file__).resolve().parent / "fonts"):
        if not d or not Path(d).is_dir():
            continue
        for f in sorted(Path(d).iterdir()):
            if f.suffix.lower() in FONT_EXT and f.name not in seen:
                seen.add(f.name)
                out.append(f)
    return out[:limit] if limit else out


def roll(rng=None, pool=None, prob=None):
    """Choose this clip's roulette: the faces it flickers through and where it lands.

    Returns None for a STATIC slam, which happens two ways. Deliberately, for
    the majority of clips — the decision is made once here, at mint time, so the
    channel always carries a mix of rolling and still marks; a flourish on every
    single bumper stops registering as one. And unavoidably, when the deployer
    has mounted too few faces to read as a roulette, since flickering between
    two fonts looks like a fault rather than a slot machine.
    """
    rng = rng or random
    pool = pool if pool is not None else font_pool()
    if len(pool) < config.ROULETTE_MIN_FONTS:
        return None
    if rng.random() >= (config.ROULETTE_PROB if prob is None else prob):
        return None
    n = min(len(pool), MAX_FACES)
    faces = rng.sample(pool, n)
    return {"faces": [str(f) for f in faces],
            "landing": str(rng.choice(faces)),
            "seed": rng.randrange(1 << 30)}


def _schedule(total=FLICKER_TOTAL):
    """Swap times across the flicker, decelerating like the player's setTimeout."""
    times, t = [], 0.0
    while t < total:
        times.append(t)
        t += INTERVAL_START + (t / total) * (INTERVAL_END - INTERVAL_START)
    return times


def face_at(spec, t, slam_start, flicker=None):
    """Which typeface is showing at time `t`.

    Before the slam it is not drawn at all; during the flicker it is whichever
    face the schedule has reached; after, it holds the landing face. `flicker`
    is the per-clip roll length, so a short bumper spins faster rather than
    running out of clip before it lands.
    """
    if spec is None or t < slam_start:
        return None
    total = FLICKER_TOTAL if flicker is None else flicker
    rel = t - slam_start
    if total <= 0 or rel >= total:
        return spec["landing"]
    # Draw WITHOUT consecutive repeats. Sampling naively lets the same face come
    # up twice in a row, and a mark that holds still for two slots reads as the
    # animation stalling rather than a reel spinning — which quietly halved the
    # number of visible changes and cost the effect most of its snap.
    rng = random.Random(spec["seed"])
    faces = spec["faces"]
    shown = None
    for mark in _schedule(total):
        if rel < mark:
            break
        nxt = faces[rng.randrange(len(faces))]
        if nxt == shown and len(faces) > 1:
            nxt = faces[(faces.index(nxt) + 1 + rng.randrange(len(faces) - 1)) % len(faces)]
        shown = nxt
    return shown or spec["landing"]


def plan(duration):
    """When the slam runs, how long it rolls, and whether it rolls at all.

    The old rule pinned the slam to a fixed tail slice, which was shorter than
    the flicker on anything under about 17 seconds — so the roulette was still
    mid-spin when the clip ended and never landed. Length is now proportional
    with a hard floor, and the flicker is shortened rather than the hold, so
    the mark always comes to rest and stays put long enough to register.
    """
    slam_len = min(max(duration * SLAM_FRACTION, SLAM_MIN),
                   SLAM_MAX, duration * SLAM_MAX_SHARE)
    start = max(0.0, duration - slam_len)
    flicker = min(FLICKER_TOTAL, slam_len - HOLD_MIN)
    return {"start": round(start, 3),
            "length": round(slam_len, 3),
            "flicker": round(max(0.0, flicker), 3),
            "hold": round(slam_len - max(0.0, flicker), 3),
            "can_roll": flicker >= FLICKER_MIN}


def slam_start_for(duration):
    return plan(duration)["start"]


def draw(img, brand, face_path, size_px, alpha=255, y=None):
    """Composite the brand mark, centred, in the given face."""
    if not face_path:
        return img
    try:
        font = ImageFont.truetype(face_path, int(size_px))
    except Exception:
        return img
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    W, H = img.size
    w = d.textlength(brand, font=font)
    yy = (H - font.size * 1.2) / 2 if y is None else y
    # A shadow keeps the mark legible over bright footage without a scrim.
    d.text(((W - w) / 2 + 3, yy + 3), brand, font=font, fill=(0, 0, 0, int(alpha * 0.55)))
    d.text(((W - w) / 2, yy), brand, font=font, fill=(255, 255, 255, alpha))
    base = img.convert("RGBA")
    return Image.alpha_composite(base, layer).convert("RGB")


def fit_size(face_path, text, max_w, start_px, min_px=28):
    """Largest size at which `text` fits `max_w` in this face.

    Decorative faces vary enormously in width — a condensed sans and a looping
    script set the same word at wildly different widths — so a single fontsize
    across a roulette makes some faces overflow the frame. Measuring per face
    keeps the mark inside the safe area whatever it lands on.
    """
    size = int(start_px)
    while size > min_px:
        try:
            f = ImageFont.truetype(face_path, size)
            if f.getbbox(text)[2] - f.getbbox(text)[0] <= max_w:
                return size
        except Exception:
            return size
        size = int(size * 0.92)
    return max(min_px, size)


def static_face(rng=None, pool=None):
    """The single face a non-rolling slam uses.

    Still chosen per clip rather than fixed, so static slams vary across the
    pool too — otherwise every non-rolling bumper would look identical.
    """
    rng = rng or random
    pool = pool if pool is not None else font_pool()
    if not pool:
        return None
    return str(rng.choice(pool))


def describe(spec):
    if spec is None:
        return "static slam"
    return "roulette over %d faces, landing on %s" % (
        len(spec["faces"]), os.path.basename(spec["landing"]))
