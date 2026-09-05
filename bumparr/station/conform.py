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
CONFORM_PROFILE_VERSION = 1
SLATE_RENDER_VERSION = 1
_LOCK = threading.Lock()


def cache_dir():
    """Where conformed segments live; under .cache so seed.py never registers them."""
    return Path(config.ASSET_ROOT) / ".cache" / "station"


def _profile_identity(*, still=False, duration=None):
    """Stable identity for every setting that affects conformed bytes.

    The version covers the fixed ffmpeg arguments in :func:`ffmpeg_command`;
    runtime settings and the still-only duration are recorded explicitly so
    persistent caches follow configuration changes without a manual purge.
    """
    return {"version": CONFORM_PROFILE_VERSION,
            "segment_seconds": config.STATION_SEGMENT_SECONDS,
            "bitrate_k": config.STATION_BITRATE_K,
            "still_duration": ("%.3f" % float(duration or 5)) if still else None}


def cache_key(row_id, mtime_ns, size, *, still=False, duration=None):
    identity = [str(row_id), int(mtime_ns), int(size),
                _profile_identity(still=still, duration=duration)]
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()[:24]


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


def _conform_file(key, row_id, src, *, still=False, duration=None, metadata=None):
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
        index.update(metadata or {})
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
    return _conform_file(cache_key(row_id, st.st_mtime_ns, st.st_size,
                                   still=still, duration=duration), row_id, src,
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
    still = row["type"] == "image"
    return cache_key(row["id"], st.st_mtime_ns, st.st_size,
                     still=still, duration=row["duration"]), src


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
    """The built-in brand slate: what airs when nothing else is conformed.

    Ten seconds of black with the brand, drawn with the same primitives as a
    station ident, so an empty pool still yields a stream that plays instead
    of a 404 that Dispatcharr treats as a dead channel. Rendered to an MP4
    through the shared frame pipe, then conformed like any other item.
    """
    from PIL import Image

    from bumparr import brandslam, ffmpeg_pipe, render_cards
    root = cache_dir()
    fps, seconds = 30, 10
    face = render_cards.fonts()[1]
    font_identity = {"configured": config.BRAND_FONT, "resolved": face}
    if face:
        try:
            fst = Path(face).stat()
            font_identity.update({"mtime_ns": fst.st_mtime_ns, "size": fst.st_size})
        except OSError:
            pass
    slate_inputs = {"version": SLATE_RENDER_VERSION, "brand": config.BRAND,
                    "font": font_identity, "width": render_cards.W,
                    "height": render_cards.H, "fps": fps, "seconds": seconds,
                    "profile": _profile_identity()}
    slate_identity = hashlib.sha256(
        json.dumps(slate_inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    # The logical id stays ``slate``, but every byte-distinct rendition gets
    # an immutable directory/URL. Existing playlists can therefore finish on
    # the old slate while a branding or profile change lands atomically.
    key = hashlib.sha256(("slate:" + slate_identity).encode("utf-8")).hexdigest()[:24]
    existing = load_index().get(SLATE_KEY, {})
    if existing.get("slate_identity") == slate_identity and existing.get("key") == key:
        return key
    if not ffmpeg_path():
        return None

    root.mkdir(parents=True, exist_ok=True)
    mp4 = root / (key + ".mp4")
    plate = Image.new("RGB", (render_cards.W, render_cards.H), "black")
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
        return _conform_file(key, SLATE_KEY, mp4, still=False,
                             metadata={"slate_identity": slate_identity,
                                       "slate_inputs": slate_inputs})
    finally:
        try:
            mp4.unlink()
        except OSError:
            pass


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
        # Every eligible row keeps its last usable rendition until a
        # replacement actually lands. This also covers a source disappearing
        # temporarily between registry health checks and the conform sweep.
        retained = {have[row["id"]]["key"] for row in rows if row["id"] in have}
        prior_slate = have.get(SLATE_KEY, {}).get("key")
        if prior_slate:
            retained.add(prior_slate)
        if stats["ffmpeg"]:
            todo = [(k, src, row) for k, src, row in current.values()
                    if have.get(row["id"], {}).get("key") != k]
            for k, src, row in (todo[:limit] if limit else todo):
                try:
                    landed = conform_source(row["id"], src, still=(row["type"] == "image"),
                                            duration=row["duration"])
                    prior = have.get(row["id"], {}).get("key")
                    if prior:
                        retained.discard(prior)
                    retained.add(landed)
                    stats["conformed"] += 1
                except Exception as e:
                    stats["failed"] += 1
                    print("[station] conform failed for %s: %s" % (row["id"], e))
            try:
                landed_slate = ensure_slate()
                if landed_slate:
                    if prior_slate:
                        retained.discard(prior_slate)
                    retained.add(landed_slate)
            except Exception as e:
                print("[station] slate failed: %s" % e)
        else:
            print("[station] ffmpeg not found; nothing conformed")
        wanted = retained | set(keep)
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
