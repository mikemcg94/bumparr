"""Combinatorial station IDs: many bumpers from few assets.

The insight this implements is that variety is cheaper than content. A handful
of backgrounds crossed with a handful of independently-rolled font roulettes
produces hundreds of station IDs that each look hand-made — 15 rolls over 15
stills and 15 clips is 450 distinct bumpers from 45 inputs.

Lengths deliberately spread from a single-frame flash up to about 12 seconds.
That range is not an aesthetic choice: the duration-fill contract needs small
denominations to make exact change, and a 0.2s brand flash is both legitimate
television grammar and the smallest useful coin in the drawer.
"""
import argparse
import json
import random
import time
import uuid
from pathlib import Path

from PIL import Image

from bumparr import brandslam, config, db, ffmpeg_pipe

# Length bands, in seconds. The short end exists to make exact fills possible;
# the long end is a proper station ident you can actually read.
BANDS = [
    (0.2, 0.6),      # flash — a single beat of brand, gone before you place it
    (1.0, 2.5),      # sting
    (3.0, 6.0),      # standard ident
    (6.0, 12.0),     # long ident, room for the roulette to land and hold
]

W, H = 1920, 1080
FPS = 30


def _sources():
    """Backgrounds the user has mounted: stills and clips, plus plain black."""
    stills, clips = [], []
    for d, bucket, exts in ((config.IMAGE_DIR, stills, (".jpg", ".jpeg", ".png", ".webp")),
                            (config.VIDEO_DIR, clips, (".mp4", ".mkv", ".webm", ".m4v"))):
        if not Path(d).is_dir():
            continue
        for f in Path(d).rglob("*"):
            if f.suffix.lower() in exts and config.OUTPUT_DIR not in f.parents:
                bucket.append(f)
    return stills, clips


def _cover(img, w=W, h=H):
    """Scale-and-center-crop to fill the frame (CSS background-size: cover),
    so any aspect ratio of source still becomes a full-bleed plate."""
    scale = max(w / img.width, h / img.height)
    img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                     Image.LANCZOS)
    left, top = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


def _dim(img, amount=0.55):
    """Darken the plate so the mark reads regardless of what is underneath."""
    black = Image.new("RGB", img.size, (0, 0, 0))
    return Image.blend(img, black, amount)


def _render_still(dest, plate, brand, spec, duration, static_face=None):
    """Encode a still-backed ident, rolling the roulette frame by frame."""
    n = max(1, round(duration * FPS))
    slam_at = 0.0 if duration < 2.0 else min(0.4, duration * 0.2)
    size = 13.0 * (min(W, H) / 100.0)

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
           "-r", str(FPS), "-i", "-",
           "-f", "lavfi", "-t", "%.3f" % duration,
           "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
           "-map", "0:v", "-map", "1:a", "-shortest",
           "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-pix_fmt", "yuv420p", "-r", str(FPS),
           "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(dest)]
    def frames():
        for i in range(n):
            t = i / float(FPS)
            if spec is not None:
                face = brandslam.face_at(spec, t, slam_at)
            else:
                face = static_face if t >= slam_at else None
            yield brandslam.draw(plate.copy(), brand, face, size).tobytes()

    ffmpeg_pipe.encode_frames(cmd, frames(), dest, timeout=300, tail=500)


def generate(count=60, seed=None, dry_run=False):
    """Produce `count` station IDs by crossing backgrounds with fresh rolls."""
    rng = random.Random(seed)
    pool = brandslam.font_pool()
    stills, clips = _sources()
    print("[station_ids] %d font(s), %d still(s), %d clip(s) available"
          % (len(pool), len(stills), len(clips)))
    if len(pool) < config.ROULETTE_MIN_FONTS:
        print("[station_ids] NOTE: too few fonts for a roulette; mount more at "
              "FONTS. Falling back to a static slam.")

    out_dir = Path(config.OUTPUT_DIR) / "station_ids"
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for i in range(count):
        lo, hi = BANDS[i % len(BANDS)]           # cycle bands for an even spread
        duration = round(rng.uniform(lo, hi), 2)
        spec = brandslam.roll(rng, pool)
        plate = None
        if stills and (not clips or rng.random() < 0.5):
            try:
                with Image.open(rng.choice(stills)) as source:
                    plate = _dim(_cover(source.convert("RGB")))
            except Exception:
                plate = None
        if plate is None:
            plate = Image.new("RGB", (W, H), (0, 0, 0))

        pid = "station_id:%d:%d:%s" % (int(time.time()), i, uuid.uuid4().hex[:8])
        name = "station_ids/%s.mp4" % pid.replace(":", "_")
        dest = Path(config.OUTPUT_DIR) / name
        # Registry uri is the SERVED path, not a filesystem path: produced output
        # is served under /media/bumpers regardless of where OUTPUT_DIR points.
        rel = "bumpers/" + name
        if dry_run:
            print("  would make %-28s %5.2fs  %s" % (name, duration, brandslam.describe(spec)))
            continue
        try:
            _render_still(dest, plate, config.BRAND, spec, duration,
                          static_face=brandslam.static_face(rng, pool))
        except Exception as e:
            print("  FAIL %s: %s" % (rel, str(e)[:160]))
            continue
        try:
            with db.conn() as c:
                cursor = c.execute(
                    """INSERT OR IGNORE INTO playables
                       (id,type,kind,source,uri,duration,title,payload,tags,weight,enabled,health,created_at)
                       VALUES (?,?,?,?,?,?,?,?,'',?,1,'ok',?)""",
                    (pid, "video", "station_id", "generated", rel, duration,
                     "%s ident" % config.BRAND,
                     json.dumps({"roulette": brandslam.describe(spec), "branded": True,
                                 "brand": config.BRAND}),
                     1.0, time.time()))
                if not cursor.rowcount:
                    raise RuntimeError("station ID registration was not inserted")
        except Exception as exc:
            try:
                dest.unlink()
            except OSError:
                pass
            print("  FAIL %s: %s" % (rel, str(exc)[:160]))
            continue
        made.append((rel, duration, spec is not None))
        print("  ok  %-40s %5.2fs  %s" % (rel, duration, brandslam.describe(spec)))
    return made


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate combinatorial station IDs.")
    ap.add_argument("--count", type=int, default=60)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    db.init_db()
    made = generate(count=a.count, seed=a.seed, dry_run=a.dry_run)
    if not a.dry_run and made:
        rolling = sum(1 for _, _, r in made if r)
        print("[station_ids] %d ident(s); lengths %.2f-%.2fs; %d rolling / %d static (%.0f%% roll)"
              % (len(made), min(d for _, d, _ in made), max(d for _, d, _ in made),
                 rolling, len(made) - rolling, 100.0 * rolling / len(made)))
