"""Turn source footage into finished, branded bumpers.

The production half of Bumparr. A source video is a QUARRY, not a bumper: one
long file yields many short clips, and once they exist the source has done its
job and can be discarded. Nothing here keeps a permanent second copy.

Three things make the output better than a random crop:

Cuts land on real shot boundaries. ffmpeg scene detection finds them where they
exist — a vintage film gives dozens — and where it finds none the source is
simply a single continuous shot, which is the normal case for modern stock
footage, so the whole file is the shot and windows are taken freely.

Windows OVERLAP. They are not a partition. The same footage can appear in two
bumpers framed differently, which multiplies yield without repeating a clip.

Lengths spread across bands on purpose. The duration-fill contract needs small
denominations to make exact change, so production deliberately mints short
clips alongside long ones rather than converging on one comfortable length.

Audio is decided by measurement, not by a coin flip: a window with real audible
sound keeps it, and a silent one gets a bed from the user's own /sounds mount
some of the time — leaving a deliberate share silent, because unbroken wall-to-
wall music is worse than the occasional held breath.
"""
import argparse
import json
import os
import random
import shutil
import subprocess
import time
from pathlib import Path

from bumparr import brandslam, config, db

FPS = 30
W, H = 1920, 1080

# Target length bands. Mirrors station_ids so the whole pool shares one coin set.
BANDS = [(2.0, 4.0), (5.0, 8.0), (8.0, 12.0), (12.0, 20.0), (20.0, 30.0)]

SCENE_THRESHOLD = float(config.env("SCENE_THRESHOLD", "0.2"))
# Below this mean volume a window is treated as having no usable sound of its own.
SILENCE_DB = float(config.env("SILENCE_DB", "-40"))
# Share of silent clips that receive a music bed. The rest stay silent on purpose.
ADD_SOUND_MIN = float(config.env("ADD_SOUND_MIN", "0.40"))
ADD_SOUND_MAX = float(config.env("ADD_SOUND_MAX", "0.75"))

SOUND_EXT = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".opus")
VIDEO_EXT = (".mp4", ".mkv", ".webm", ".m4v", ".mov", ".avi")

# Source directories holding EPHEMERAL captures, which must never be quarried.
# A live-window snippet is a picture of right now: the camera is interesting
# precisely because it keeps changing, and each capture replaces the last. Cutting
# permanent clips from one would freeze a moment that was meant to expire, and
# those clips would then accumulate forever while the live view moved on.
EPHEMERAL_DIRS = set(
    d.strip() for d in config.env("EPHEMERAL_DIRS", "windows").split(",")
    if d.strip())

# Directories holding Bumparr's OWN FINISHED OUTPUT. These are not source
# material and must never be quarried: a rendered text card is a completed
# bumper, and cutting windows out of one produces fragments of a card that was
# already exactly the right length. The output tree is excluded separately by
# path, but rendered cards live beside the sources rather than under it.
OUTPUT_DIRS = set(
    d.strip() for d in config.env("OUTPUT_DIRS", "cards,bumpers").split(",")
    if d.strip())


# ------------------------------------------------------------------ probing --
def duration_of(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def dimensions_of(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True)
    try:
        w, h = out.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except Exception:
        return 0, 0


def scene_cuts(path, threshold=SCENE_THRESHOLD):
    """Timestamps of real shot changes. Empty means the source is one shot."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-filter:v", "select='gt(scene,%s)',showinfo" % threshold, "-f", "null", "-"],
        capture_output=True, text=True, timeout=1800)
    cuts = []
    for ln in r.stderr.splitlines():
        if "pts_time:" in ln:
            try:
                cuts.append(float(ln.split("pts_time:")[1].split()[0]))
            except Exception:
                pass
    return sorted(cuts)


def mean_volume(path, start=None, dur=None):
    """Mean volume in dBFS, or None if unmeasurable.

    `volumedetect` reports at info level, so the log level must NOT be raised to
    error here or the very line being parsed is suppressed.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostats"]
    if start is not None:
        cmd += ["-ss", "%.3f" % start]
    cmd += ["-i", str(path)]
    if dur is not None:
        cmd += ["-t", "%.3f" % dur]
    cmd += ["-af", "volumedetect", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    for ln in r.stderr.splitlines():
        if "mean_volume:" in ln:
            try:
                return float(ln.split("mean_volume:")[1].strip().split()[0])
            except Exception:
                return None
    return None


# ----------------------------------------------------------------- planning --
def plan_windows(src_duration, cuts, count, rng, band_offset=0):
    """Choose overlapping [start, length] windows, snapped to shot boundaries.

    With scene cuts available a window runs from one cut to a later one, so it
    opens and closes on a real edit. Without them any offset is fair game, since
    the source is a single continuous shot.
    """
    windows = []
    usable = max(0.0, src_duration - 0.3)
    if usable <= 1.0:
        return windows

    boundaries = [c for c in cuts if 0.2 < c < usable]
    for i in range(count):
        # Bands cycle across the whole RUN, not within one source. Cycling per
        # source meant every short file produced only band-0 clips and the pool
        # converged on one length, which is exactly what the fill contract
        # cannot work with.
        lo, hi = BANDS[(band_offset + i) % len(BANDS)]
        hi = min(hi, usable)
        if hi <= lo:
            lo = max(1.0, hi * 0.5)
        target = rng.uniform(lo, hi)

        if boundaries:
            start = rng.choice(boundaries)
            # Prefer ending on a later cut close to the target length.
            after = [c for c in boundaries if c > start + 1.0]
            if after:
                end = min(after, key=lambda c: abs((c - start) - target))
                length = end - start
                if length > 30.0:                 # a long scene: take a slice of it
                    length = target
            else:
                length = target
        else:
            length = target
            start = rng.uniform(0, max(0.1, usable - length))

        length = max(1.0, min(length, usable - start, 30.0))
        if length < 1.0:
            continue
        windows.append((round(start, 2), round(length, 2)))
    return windows


def clips_for_duration(src_duration):
    """How many clips a source is worth.

    Windows overlap, so yield is not bounded by dividing the runtime into
    slices: a 24s stock clip legitimately gives several differently-framed
    bumpers. Long films are capped so one quarry cannot flood the pool.
    """
    if src_duration <= 0:
        return 0
    return max(2, min(40, int(src_duration / 12.0)))


# ------------------------------------------------------------------ audio ----
def sound_pool():
    d = Path(config.SOUND_DIR)
    if not d.is_dir():
        return []
    return [f for f in sorted(d.rglob("*")) if f.suffix.lower() in SOUND_EXT]


def _escape(p):
    """Escape a path for use inside an ffmpeg filter argument."""
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


# ------------------------------------------------------------- the slam ------
def _drawtext_chain(brand, spec, static_face, duration):
    """Build timed drawtext filters that perform the roulette inside ffmpeg.

    Piping frames through Python works but decodes and re-encodes every pixel;
    for video sources it is far cheaper to let ffmpeg draw the mark, switching
    typeface on a schedule with `enable=between(...)`. Same animation, a
    fraction of the cost.
    """
    sp = brandslam.plan(duration)
    slam_at, flicker = sp["start"], sp["flicker"]
    base_px = int(13.0 * (min(W, H) / 100.0))
    safe_w = int(W * 0.82)          # keep the mark inside the title-safe area

    def entry(face, enable):
        size = brandslam.fit_size(face, brand, safe_w, base_px)
        return ("drawtext=fontfile='%s':text='%s':x=(w-text_w)/2:y=(h-text_h)/2"
                ":fontsize=%d:fontcolor=white:shadowcolor=black@0.6:shadowx=3:shadowy=3"
                ":enable='%s'" % (_escape(face), brand, size, enable))

    parts = []
    if spec is None:
        if not static_face:
            return []
        return [entry(static_face, "gte(t,%.3f)" % slam_at)]

    if not sp["can_roll"]:
        # Too little room for a believable spin; a still mark reads as intent.
        return [entry(static_face or spec["landing"], "gte(t,%.3f)" % slam_at)]

    marks = brandslam._schedule(flicker)
    for i, m in enumerate(marks):
        face = brandslam.face_at(spec, slam_at + m + 0.001, slam_at, flicker)
        start = slam_at + m
        end = slam_at + (marks[i + 1] if i + 1 < len(marks) else flicker)
        if start >= duration:
            break
        parts.append(entry(face, "between(t,%.3f,%.3f)" % (start, min(end, duration))))
    parts.append(entry(spec["landing"], "gte(t,%.3f)" % (slam_at + flicker)))
    return parts


# ---------------------------------------------------------------- extract ----
def _video_chain(src_w, src_h, brand, spec, static_face, length):
    """Fit the source to the frame, then brand it.

    Phone-shot stock is often vertical, and pillarboxing it leaves half the
    screen black, which reads as a broken file rather than a style. A blurred,
    darkened copy of the same footage fills the sides instead, so a portrait
    source still looks composed. Sources already near 16:9 skip that work
    entirely, since the foreground would cover it anyway.
    """
    tail = _drawtext_chain(brand, spec, static_face, length)
    tail += ["fade=in:st=0:d=0.3", "fade=out:st=%.2f:d=0.4" % max(0.0, length - 0.4),
             "format=yuv420p"]
    target = W / float(H)
    ratio = (src_w / float(src_h)) if src_w and src_h else target

    if abs(ratio - target) < 0.06:
        chain = ["scale=%d:%d:force_original_aspect_ratio=decrease" % (W, H),
                 "pad=%d:%d:(ow-iw)/2:(oh-ih)/2" % (W, H), "fps=%d" % FPS]
        return "[0:v]" + ",".join(chain + tail) + "[v]"

    # Blur at a fraction of the output size, then scale up. A blur discards
    # detail by definition, so producing it at full resolution buys nothing and
    # costs several 1080p frame buffers per filter stage — enough to get the
    # process OOM-killed on a host without swap. Small-then-upscale is visually
    # indistinguishable and an order of magnitude cheaper.
    bw, bh = W // 4, H // 4
    return (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
        "gblur=sigma=7,eq=brightness=-0.14:saturation=0.75,scale=%d:%d[bgb];"
        "[fg]scale=%d:%d:force_original_aspect_ratio=decrease[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,fps=%d,%s[v]"
        % (bw, bh, bw, bh, W, H, W, H, FPS, ",".join(tail))
    )


def cut_clip(src, dest, start, length, brand, spec, static_face, bed=None, native=True):
    """Cut one window, brand it, and resolve its audio in a single encode."""
    src_w, src_h = dimensions_of(src)
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-ss", "%.3f" % start, "-i", str(src), "-t", "%.3f" % length]
    if bed:
        # Start the bed at a random offset so repeated use of one track does not
        # always open on the same bar.
        cmd += ["-ss", "%.3f" % bed["offset"], "-i", str(bed["path"])]

    filters = [_video_chain(src_w, src_h, brand, spec, static_face, length)]
    maps = ["-map", "[v]"]
    if bed:
        filters.append("[1:a]volume=%.2f,afade=in:st=0:d=0.4,afade=out:st=%.2f:d=0.6[a]"
                       % (bed["volume"], max(0.0, length - 0.6)))
        maps += ["-map", "[a]"]
    elif native:
        filters.append("[0:a]afade=in:st=0:d=0.3,afade=out:st=%.2f:d=0.4[a]"
                       % max(0.0, length - 0.4))
        maps += ["-map", "[a]"]
    else:
        cmd += ["-f", "lavfi", "-t", "%.3f" % length,
                "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        maps += ["-map", "%d:a" % (2 if bed else 1)]

    cmd += ["-filter_complex", ";".join(filters)] + maps
    # Bounded threads: x264 allocates per-thread frame buffers, and the default
    # (one per core) is a large multiplier on a shared host that is already tight.
    cmd += ["-t", "%.3f" % length, "-threads", "2",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "128k",
            "-ar", "48000", "-ac", "2", "-shortest",
            "-movflags", "+faststart", str(dest)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "")[-500:])


# ------------------------------------------------------------------ driver ---
def weight_index(default=0.9):
    """Source weights for the whole run, read once.

    This used to be a query per source. Under concurrency that is both wasteful
    and dangerous: opening a fresh connection for every file while the card
    renderer was writing produced `database is locked`, and because the lookup
    sat outside any per-source guard, one lock error aborted an entire pass
    after 23 of 287 sources. Reading the table once removes the contention
    window and the failure mode together.

    Returns (by_uri, by_kind) so a clip can inherit its exact source's weight,
    or failing that its category's.
    """
    by_uri, by_kind = {}, {}
    with db.conn() as c:
        rows = c.execute(
            "SELECT uri, kind, weight, payload FROM playables "
            "WHERE source != 'produced' AND uri IS NOT NULL").fetchall()
    for r in rows:
        try:
            base = json.loads(r["payload"] or "{}").get("base_weight")
        except Exception:
            base = None
        w = float(base if base is not None else (r["weight"] or default))
        by_uri[r["uri"]] = w
        # Lowest weight in a category is the conservative choice: a produced
        # clip should never outrank the least-wanted material it came from.
        if r["kind"] not in by_kind or w < by_kind[r["kind"]]:
            by_kind[r["kind"]] = w
    return by_uri, by_kind


def produce_from_source(src, kind, rng, pool, sounds, weights, delete_source=False,
                        count=None, band_offset=0):
    """Cut, brand and register every clip a single source is worth."""
    src = Path(src)
    src_dur = duration_of(src)
    if src_dur <= 1.0:
        return [], "unreadable or too short"

    try:
        src_rel = str(src.relative_to(Path(config.VIDEO_DIR)))
    except Exception:
        src_rel = src.name
    by_uri, by_kind = weights
    weight = by_uri.get(src_rel, by_kind.get(kind, 0.9))
    cuts = scene_cuts(src)
    n = count or clips_for_duration(src_dur)
    windows = plan_windows(src_dur, cuts, n, rng, band_offset=band_offset)
    out_dir = Path(config.OUTPUT_DIR) / kind
    out_dir.mkdir(parents=True, exist_ok=True)

    made = []
    for i, (start, length) in enumerate(windows):
        vol = mean_volume(src, start, length)
        has_native = vol is not None and vol > SILENCE_DB
        bed = None
        if not has_native and sounds:
            if rng.random() < rng.uniform(ADD_SOUND_MIN, ADD_SOUND_MAX):
                track = rng.choice(sounds)
                tdur = duration_of(track)
                bed = {"path": track,
                       "offset": round(rng.uniform(0, max(0.1, tdur - length - 1)), 2)
                       if tdur > length + 2 else 0.0,
                       "volume": round(rng.uniform(0.22, 0.40), 2)}

        spec = brandslam.roll(rng, pool)
        static = brandslam.static_face(rng, pool)
        stem = "%s_%s_%d" % (kind, src.stem[:38].replace(" ", "_"), i)
        name = "%s/%s.mp4" % (kind, stem)
        dest = Path(config.OUTPUT_DIR) / name
        try:
            cut_clip(src, dest, start, length, config.BRAND, spec, static,
                     bed=bed, native=has_native)
        except Exception as e:
            print("    FAIL %-42s %s" % (stem, str(e)[:120]))
            continue
        actual = duration_of(dest)
        audio = ("native" if has_native else
                 ("bed:" + Path(bed["path"]).stem[:18] if bed else "silent"))
        with db.conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO playables
                   (id,type,kind,source,uri,duration,title,payload,tags,weight,enabled,health,created_at)
                   VALUES (?,?,?,?,?,?,?,?,'',?,1,'ok',?)""",
                ("clip:%s:%d" % (stem, int(time.time())), "video", kind, "produced",
                 "bumpers/" + name, actual, src.stem.replace("_", " ")[:70],
                 json.dumps({"from": src.name, "window": [start, length],
                             "audio": audio, "slam": brandslam.describe(spec),
                             "scene_cuts": len(cuts),
                             # The mark is IN the file. Players must not draw
                             # their own over it, or the clip ends up branded
                             # twice with two different rolls fighting.
                             "branded": True,
                             # The brand actually baked into this file. Two
                             # containers sharing one pool can disagree on
                             # BRAND, and a file stamped with the wrong name is
                             # invisible until someone watches it — so record
                             # it and let a checker find the mismatches.
                             "brand": config.BRAND,
                             # Pre-seasonal weight, so the hourly seasonal pass
                             # has a stable baseline to multiply rather than
                             # compounding whatever was current at mint time.
                             "base_weight": weight}),
                 weight, time.time()))
            c.commit()
        made.append((name, actual, audio, spec is not None))
        print("    ok  %-44s %5.2fs  %-22s %s"
              % (stem[:44], actual, audio, brandslam.describe(spec)))

    if made and delete_source:
        try:
            src.unlink()
            print("    source deleted (%d clip(s) extracted)" % len(made))
        except Exception as e:
            print("    source NOT deleted: %s" % e)
    return made, None


def run(category=None, limit=None, delete_source=False, seed=None, per_source=None):
    rng = random.Random(seed)
    pool = brandslam.font_pool()
    sounds = sound_pool()
    root = Path(config.VIDEO_DIR)
    out_root = Path(config.OUTPUT_DIR)

    sources = []
    for f in sorted(root.rglob("*")):
        if f.suffix.lower() not in VIDEO_EXT or not f.is_file():
            continue
        if out_root in f.parents or f.parent.name.startswith("."):
            continue           # never re-process our own output
        cat = f.parent.name
        if cat in EPHEMERAL_DIRS:
            continue           # live captures expire; see EPHEMERAL_DIRS
        if cat in OUTPUT_DIRS or any(part in OUTPUT_DIRS for part in f.parts[:-1]):
            continue           # our own finished output; see OUTPUT_DIRS
        if category and cat != category:
            continue
        sources.append((f, cat))
    if limit:
        sources = sources[:limit]

    print("[produce] %d source(s); %d font(s); %d sound(s); delete_source=%s"
          % (len(sources), len(pool), len(sounds), delete_source))
    if len(pool) < config.ROULETTE_MIN_FONTS:
        # Loud, because the roulette is the signature and silently losing it to
        # a missing mount is the kind of thing you only notice on screen.
        print("[produce] WARNING: only %d font(s) available, below the %d needed for a "
              "roulette — every clip will get a STATIC slam. Mount your typefaces at "
              "FONTS to restore it." % (len(pool), config.ROULETTE_MIN_FONTS))
    if EPHEMERAL_DIRS:
        print("[produce] skipping ephemeral live-capture dir(s): %s"
              % ", ".join(sorted(EPHEMERAL_DIRS)))
    if not sounds:
        print("[produce] NOTE: no audio in SOUNDS — silent clips stay silent.")

    weights = weight_index()
    total, rolling, band_offset, failures = [], 0, 0, []
    for src, cat in sources:
        print("  %s  (%.1fs)" % (src.name[:60], duration_of(src)))
        # One bad source must never end the run. A batch over hundreds of files
        # will hit a locked database, an unreadable clip, or a codec ffmpeg
        # dislikes; losing the remaining files to any of those wastes the whole
        # pass, which is exactly what happened at 23 of 287 sources.
        try:
            made, err = produce_from_source(src, cat, rng, pool, sounds, weights,
                                            delete_source=delete_source, count=per_source,
                                            band_offset=band_offset)
        except Exception as e:
            failures.append("%s: %s" % (src.name, e))
            print("    SKIPPED %s: %s" % (src.name[:44], str(e)[:110]))
            band_offset += 1
            continue
        band_offset += len(made) or 1
        if err:
            print("    skipped: %s" % err)
        total += made
        rolling += sum(1 for m in made if m[3])
    if failures:
        print("[produce] %d source(s) skipped:" % len(failures))
        for f in failures[:10]:
            print("    %s" % f[:150])
    if total:
        lens = sorted(m[1] for m in total)
        audio = {}
        for m in total:
            key = m[2].split(":")[0]
            audio[key] = audio.get(key, 0) + 1
        print("[produce] %d clip(s); %.2f-%.2fs; audio %s; %d rolling / %d static"
              % (len(total), lens[0], lens[-1], audio, rolling, len(total) - rolling))
    return total


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Cut, brand and register bumpers from source footage.")
    ap.add_argument("--category", help="only this source sub-directory")
    ap.add_argument("--limit", type=int, help="max source files")
    ap.add_argument("--per-source", type=int, help="override clips per source")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--delete-source", action="store_true",
                    help="remove each source after its clips are written. Irreversible.")
    a = ap.parse_args()
    db.init_db()
    t0 = time.time()
    run(category=a.category, limit=a.limit, delete_source=a.delete_source,
        seed=a.seed, per_source=a.per_source)
    print("[produce] done in %.1fs" % (time.time() - t0))
