import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bumparr import config, db
from bumparr.station import conform


class _Proc:
    """A fake ffmpeg: writes the segment list the real one would, then exits."""
    def __init__(self, out_dir, segs=(4.0, 4.0, 2.07), returncode=0, hang=False):
        self.out_dir, self.segs, self.returncode, self.hang = Path(out_dir), segs, returncode, hang
        self.killed = False

    def communicate(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        if self.returncode == 0 and not self.killed:
            lines = ["#EXTM3U"]
            for i, d in enumerate(self.segs):
                (self.out_dir / ("%03d.ts" % i)).write_bytes(b"ts")
                lines += ["#EXTINF:%.6f," % d, "%03d.ts" % i]
            (self.out_dir / "segments.m3u8").write_text("\n".join(lines), encoding="utf-8")
        return b"", b"diag tail"

    def kill(self):
        self.killed = True; self.returncode = -9


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.originals = config.DB_PATH, config.ASSET_ROOT, config.OUTPUT_DIR
        config.DB_PATH = str(Path(self.tmp.name) / "s.db")
        config.ASSET_ROOT = Path(self.tmp.name) / "assets"
        config.OUTPUT_DIR = config.ASSET_ROOT / "bumpers"
        config.ASSET_ROOT.mkdir(); config.OUTPUT_DIR.mkdir()
        for attr, value in zip(("DB_PATH", "ASSET_ROOT", "OUTPUT_DIR"), self.originals):
            self.addCleanup(setattr, config, attr, value)
        db.init_db()
        self.src = config.OUTPUT_DIR / "clip.mp4"; self.src.write_bytes(b"x" * 100)

    def popen(self, **kw):
        def fake(cmd, **_):
            out_dir = Path(cmd[-1]).parent
            return _Proc(out_dir, **kw)
        return mock.patch.object(subprocess, "Popen", side_effect=fake)


class Command(Base):
    def test_audio_source_maps_its_own_track_and_pads(self):
        cmd = conform.ffmpeg_command(self.src, Path("/o"), audio=True)
        self.assertIn("0:a:0", cmd); self.assertNotIn("anullsrc=r=48000:cl=stereo", cmd)
        self.assertIn("aresample=async=1,apad", cmd)
        self.assertEqual(cmd[-1], "/o/%03d.ts")
        self.assertIn("-segment_time", cmd); self.assertIn(str(config.STATION_SEGMENT_SECONDS), cmd)

    def test_silent_source_gets_a_synthesized_track(self):
        cmd = conform.ffmpeg_command(self.src, Path("/o"), audio=False)
        self.assertIn("anullsrc=r=48000:cl=stereo", cmd); self.assertIn("1:a:0", cmd)

    def test_still_loops_for_its_duration(self):
        cmd = conform.ffmpeg_command(self.src, Path("/o"), audio=False, still=True, duration=7.5)
        self.assertEqual(cmd[cmd.index("-loop") + 1], "1")
        self.assertEqual(cmd[cmd.index("-t") + 1], "7.500")

    def test_profile_is_fixed(self):
        cmd = conform.ffmpeg_command(self.src, Path("/o"), audio=True)
        for flag, value in (("-g", "60"), ("-sc_threshold", "0"), ("-profile:v", "high"),
                            ("-level", "4.1"), ("-ar", "48000"), ("-ac", "2")):
            self.assertEqual(cmd[cmd.index(flag) + 1], value)
        self.assertIn("scale=1920:1080:force_original_aspect_ratio=decrease", " ".join(cmd))


class ConformOne(Base):
    def test_lands_atomically_with_index(self):
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=True):
            key = conform.conform_source("vid:clip", self.src)
        d = conform.cache_dir() / key
        idx = json.loads((d / "index.json").read_text())
        self.assertEqual(idx["id"], "vid:clip"); self.assertEqual(idx["key"], key)
        self.assertEqual(idx["segments"], [4.0, 4.0, 2.07]); self.assertAlmostEqual(idx["duration"], 10.07)
        self.assertTrue((d / "002.ts").exists()); self.assertFalse((d / "segments.m3u8").exists())
        self.assertFalse((conform.cache_dir() / (key + ".part")).exists())

    def test_is_idempotent_for_an_unchanged_source(self):
        with self.popen() as p, mock.patch.object(conform, "has_audio", return_value=True):
            k1 = conform.conform_source("vid:clip", self.src)
            k2 = conform.conform_source("vid:clip", self.src)
        self.assertEqual(k1, k2); self.assertEqual(p.call_count, 1)

    def test_nonzero_exit_leaves_nothing_and_reports_tail(self):
        with self.popen(returncode=1), mock.patch.object(conform, "has_audio", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "diag tail"):
                conform.conform_source("vid:clip", self.src)
        self.assertEqual(list(conform.cache_dir().iterdir()), [])

    def test_timeout_kills_and_cleans(self):
        with self.popen(hang=True), mock.patch.object(conform, "has_audio", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                conform.conform_source("vid:clip", self.src)
        self.assertEqual(list(conform.cache_dir().iterdir()), [])

    def test_key_changes_with_the_source(self):
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=False):
            k1 = conform.conform_source("vid:clip", self.src)
            self.src.write_bytes(b"y" * 200)
            k2 = conform.conform_source("vid:clip", self.src)
        self.assertNotEqual(k1, k2)

    def test_key_changes_with_runtime_profile(self):
        st = self.src.stat()
        original = conform.cache_key("vid:clip", st.st_mtime_ns, st.st_size)
        with mock.patch.object(config, "STATION_SEGMENT_SECONDS",
                               config.STATION_SEGMENT_SECONDS + 1):
            segment_changed = conform.cache_key("vid:clip", st.st_mtime_ns, st.st_size)
        with mock.patch.object(config, "STATION_BITRATE_K", config.STATION_BITRATE_K + 1):
            bitrate_changed = conform.cache_key("vid:clip", st.st_mtime_ns, st.st_size)
        self.assertNotEqual(original, segment_changed)
        self.assertNotEqual(original, bitrate_changed)

    def test_image_duration_is_part_of_its_key(self):
        st = self.src.stat()
        short = conform.cache_key("img:clip", st.st_mtime_ns, st.st_size,
                                  still=True, duration=5)
        long = conform.cache_key("img:clip", st.st_mtime_ns, st.st_size,
                                 still=True, duration=8)
        self.assertNotEqual(short, long)


class Sweep(Base):
    def seed(self, rows):
        with db.conn() as c:
            for r in rows:
                c.execute("INSERT INTO playables (id,type,kind,uri,duration,enabled,health) VALUES (?,?,?,?,?,?,?)", r)
            c.commit()

    def test_conforms_eligible_prunes_stale_and_skips_streams(self):
        (config.ASSET_ROOT / "still.png").write_bytes(b"png")
        self.seed([("vid:clip", "video", "ambient", "bumpers/clip.mp4", 10, 1, "ok"),
                   ("img:still.png", "image", "art", "still.png", 8, 1, "ok"),
                   ("stream:cam", "stream", "webcam", "https://x/y.m3u8", 0, 1, "ok"),
                   ("vid:off", "video", "ambient", "bumpers/clip.mp4", 10, 0, "ok")])
        stale = conform.cache_dir() / "deadbeefdeadbeefdeadbeef"; stale.mkdir(parents=True)
        (stale / "index.json").write_text(json.dumps({"id": "gone", "key": stale.name, "segments": [1], "duration": 1, "conformed_at": 1}))
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=False), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"):
            stats = conform.sweep()
        self.assertEqual((stats["conformed"], stats["failed"], stats["pruned"]), (2, 0, 1))
        self.assertFalse(stale.exists())
        self.assertEqual(set(conform.load_index()), {"vid:clip", "img:still.png"})

    def test_one_failure_does_not_stop_the_sweep(self):
        (config.OUTPUT_DIR / "bad.mp4").write_bytes(b"bad")
        self.seed([("vid:bad", "video", "a", "bumpers/bad.mp4", 5, 1, "ok"),
                   ("vid:clip", "video", "a", "bumpers/clip.mp4", 5, 1, "ok")])
        calls = {"n": 0}
        def fake(cmd, **_):
            calls["n"] += 1
            return _Proc(Path(cmd[-1]).parent, returncode=1 if "bad" in " ".join(cmd) else 0)
        with mock.patch.object(subprocess, "Popen", side_effect=fake), \
                mock.patch.object(conform, "has_audio", return_value=False), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"):
            stats = conform.sweep()
        self.assertEqual((stats["conformed"], stats["failed"]), (1, 1))

    def test_keep_protects_keys_on_air(self):
        live = conform.cache_dir() / "aaaaaaaaaaaaaaaaaaaaaaaa"; live.mkdir(parents=True)
        (live / "index.json").write_text(json.dumps({"id": "gone", "key": live.name, "segments": [1], "duration": 1, "conformed_at": 1}))
        with mock.patch.object(conform, "ffmpeg_path", return_value=None):
            stats = conform.sweep(keep={live.name})
        self.assertEqual(stats["pruned"], 0); self.assertTrue(live.exists()); self.assertFalse(stats["ffmpeg"])

    def test_limit_bounds_a_pass(self):
        (config.OUTPUT_DIR / "b.mp4").write_bytes(b"b")
        self.seed([("vid:a", "video", "a", "bumpers/clip.mp4", 5, 1, "ok"),
                   ("vid:b", "video", "a", "bumpers/b.mp4", 5, 1, "ok")])
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=False), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"):
            self.assertEqual(conform.sweep(limit=1)["conformed"], 1)

    def test_limit_keeps_prior_rendition_for_deferred_replacement(self):
        (config.OUTPUT_DIR / "first.mp4").write_bytes(b"first")
        self.seed([("vid:first", "video", "a", "bumpers/first.mp4", 5, 1, "ok"),
                   ("vid:clip", "video", "a", "bumpers/clip.mp4", 5, 1, "ok")])
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=False):
            prior = conform.conform_source("vid:clip", self.src)
        self.src.write_bytes(b"replacement" * 20)
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=False), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(conform, "ensure_slate", return_value=None):
            stats = conform.sweep(limit=1)
        self.assertEqual(stats["conformed"], 1)
        self.assertTrue((conform.cache_dir() / prior).is_dir())
        self.assertEqual(conform.load_index()["vid:clip"]["key"], prior)

    def test_failed_replacement_keeps_prior_rendition(self):
        self.seed([("vid:clip", "video", "a", "bumpers/clip.mp4", 5, 1, "ok")])
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=False):
            prior = conform.conform_source("vid:clip", self.src)
        self.src.write_bytes(b"replacement" * 20)
        with self.popen(returncode=1), mock.patch.object(conform, "has_audio", return_value=False), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(conform, "ensure_slate", return_value=None):
            stats = conform.sweep()
        self.assertEqual(stats["failed"], 1)
        self.assertTrue((conform.cache_dir() / prior).is_dir())
        self.assertEqual(conform.load_index()["vid:clip"]["key"], prior)

    def test_missing_source_keeps_prior_rendition_for_eligible_row(self):
        self.seed([("vid:clip", "video", "a", "bumpers/clip.mp4", 5, 1, "ok")])
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=False):
            prior = conform.conform_source("vid:clip", self.src)
        self.src.unlink()
        with mock.patch.object(conform, "ffmpeg_path", return_value=None):
            stats = conform.sweep()
        self.assertEqual(stats["skipped"], 1)
        self.assertTrue((conform.cache_dir() / prior).is_dir())
        self.assertEqual(conform.load_index()["vid:clip"]["key"], prior)

    def test_source_change_during_sweep_keeps_actual_landed_key(self):
        self.seed([("vid:clip", "video", "a", "bumpers/clip.mp4", 5, 1, "ok")])
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=False):
            prior = conform.conform_source("vid:clip", self.src)
        self.src.write_bytes(b"first replacement")
        real_expected = conform.expected_key
        changed = {"done": False}

        def change_after_expected(row):
            result = real_expected(row)
            if not changed["done"]:
                self.src.write_bytes(b"second replacement is a different size")
                changed["done"] = True
            return result

        with self.popen(), mock.patch.object(conform, "has_audio", return_value=False), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(conform, "expected_key", side_effect=change_after_expected), \
                mock.patch.object(conform, "ensure_slate", return_value=None):
            stats = conform.sweep()
        landed = conform.load_index()["vid:clip"]["key"]
        self.assertEqual(stats["conformed"], 1)
        self.assertNotEqual(landed, prior)
        self.assertTrue((conform.cache_dir() / landed).is_dir())
        self.assertFalse((conform.cache_dir() / prior).exists())


class Slate(Base):
    def test_slate_is_rendered_then_conformed_once(self):
        encoded = {"n": 0}

        def fake_encode(cmd, frames, dest, *, timeout, tail=600):
            encoded["n"] += 1
            list(frames)
            Path(dest).write_bytes(b"mp4")

        from bumparr import ffmpeg_pipe
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=True), \
                mock.patch.object(ffmpeg_pipe, "encode_frames", side_effect=fake_encode), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
                mock.patch("bumparr.render_cards.fonts", return_value=(None, None)):
            key = conform.ensure_slate()
            self.assertEqual(conform.ensure_slate(), key)
        self.assertEqual(encoded["n"], 1)
        idx = json.loads((conform.cache_dir() / key / "index.json").read_text())
        self.assertEqual(idx["id"], "slate")
        self.assertEqual(idx["key"], key)
        self.assertIn("slate_identity", idx)
        self.assertFalse((conform.cache_dir() / (key + ".mp4")).exists())

    def test_brand_and_font_changes_regenerate_slate(self):
        encoded = {"n": 0}

        def fake_encode(cmd, frames, dest, *, timeout, tail=600):
            encoded["n"] += 1
            list(frames)
            Path(dest).write_bytes(b"mp4")

        from bumparr import ffmpeg_pipe
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=True), \
                mock.patch.object(ffmpeg_pipe, "encode_frames", side_effect=fake_encode), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
                mock.patch("bumparr.render_cards.fonts", return_value=(None, None)), \
                mock.patch.object(config, "BRAND", "FIRST"), \
                mock.patch.object(config, "BRAND_FONT", "first.ttf"):
            first_key = conform.ensure_slate()
            first = json.loads((conform.cache_dir() / first_key / "index.json").read_text())
            config.BRAND = "SECOND"
            brand_key = conform.ensure_slate()
            config.BRAND_FONT = "second.ttf"
            final_key = conform.ensure_slate()
        final = json.loads((conform.cache_dir() / final_key / "index.json").read_text())
        self.assertEqual(encoded["n"], 3)
        self.assertEqual(len({first_key, brand_key, final_key}), 3)
        self.assertNotEqual(first["slate_identity"], final["slate_identity"])
        self.assertEqual(final["slate_inputs"]["brand"], "SECOND")
        self.assertEqual(final["slate_inputs"]["font"]["configured"], "second.ttf")
        self.assertTrue((conform.cache_dir() / first_key).is_dir())

    def test_failed_slate_refresh_preserves_previous_slate(self):
        def fake_encode(cmd, frames, dest, *, timeout, tail=600):
            list(frames)
            Path(dest).write_bytes(b"mp4")

        from bumparr import ffmpeg_pipe
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=True), \
                mock.patch.object(ffmpeg_pipe, "encode_frames", side_effect=fake_encode), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
                mock.patch("bumparr.render_cards.fonts", return_value=(None, None)), \
                mock.patch.object(config, "BRAND", "FIRST"):
            prior_key = conform.ensure_slate()
        index_path = conform.cache_dir() / prior_key / "index.json"
        before = json.loads(index_path.read_text())
        with self.popen(returncode=1), mock.patch.object(conform, "has_audio", return_value=True), \
                mock.patch.object(ffmpeg_pipe, "encode_frames", side_effect=fake_encode), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
                mock.patch("bumparr.render_cards.fonts", return_value=(None, None)), \
                mock.patch.object(config, "BRAND", "SECOND"):
            with self.assertRaises(RuntimeError):
                conform.ensure_slate()
        after = json.loads(index_path.read_text())
        self.assertEqual(after["slate_identity"], before["slate_identity"])
        self.assertEqual(conform.load_index()["slate"]["key"], prior_key)

    def test_sweep_keeps_active_old_slate_after_refresh(self):
        def fake_encode(cmd, frames, dest, *, timeout, tail=600):
            list(frames)
            Path(dest).write_bytes(b"mp4")

        from bumparr import ffmpeg_pipe
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=True), \
                mock.patch.object(ffmpeg_pipe, "encode_frames", side_effect=fake_encode), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
                mock.patch("bumparr.render_cards.fonts", return_value=(None, None)), \
                mock.patch.object(config, "BRAND", "FIRST"):
            old_key = conform.ensure_slate()
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=True), \
                mock.patch.object(ffmpeg_pipe, "encode_frames", side_effect=fake_encode), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
                mock.patch("bumparr.render_cards.fonts", return_value=(None, None)), \
                mock.patch.object(config, "BRAND", "SECOND"):
            conform.sweep(keep={old_key})
        new_key = conform.load_index()["slate"]["key"]
        self.assertNotEqual(new_key, old_key)
        self.assertTrue((conform.cache_dir() / old_key).is_dir())
        self.assertTrue((conform.cache_dir() / new_key).is_dir())

    def test_profile_refresh_does_not_mutate_old_slate(self):
        def fake_encode(cmd, frames, dest, *, timeout, tail=600):
            list(frames)
            Path(dest).write_bytes(b"mp4")

        from bumparr import ffmpeg_pipe
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=True), \
                mock.patch.object(ffmpeg_pipe, "encode_frames", side_effect=fake_encode), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
                mock.patch("bumparr.render_cards.fonts", return_value=(None, None)):
            old_key = conform.ensure_slate()
        old_index = (conform.cache_dir() / old_key / "index.json").read_bytes()
        with self.popen(), mock.patch.object(conform, "has_audio", return_value=True), \
                mock.patch.object(ffmpeg_pipe, "encode_frames", side_effect=fake_encode), \
                mock.patch.object(conform, "ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
                mock.patch("bumparr.render_cards.fonts", return_value=(None, None)), \
                mock.patch.object(config, "STATION_SEGMENT_SECONDS",
                                  config.STATION_SEGMENT_SECONDS + 1):
            new_key = conform.ensure_slate()
        self.assertNotEqual(new_key, old_key)
        self.assertEqual((conform.cache_dir() / old_key / "index.json").read_bytes(), old_index)

    def test_slate_needs_ffmpeg(self):
        with mock.patch.object(conform, "ffmpeg_path", return_value=None):
            self.assertIsNone(conform.ensure_slate())


if __name__ == "__main__":
    unittest.main()
