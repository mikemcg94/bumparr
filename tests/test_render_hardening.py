import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from PIL import Image

from bumparr import (
    brandslam,
    config,
    db,
    produce,
    render_cards,
    station_ids,
    stream_proxy,
)


class RenderHardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.assets = config.ASSET_ROOT
        config.ASSET_ROOT = Path(self.tmp.name) / "assets"
        config.ASSET_ROOT.mkdir()
        self.addCleanup(setattr, config, "ASSET_ROOT", self.assets)

    def test_music_bed_containment_including_symlink(self):
        good = config.ASSET_ROOT / "music" / "good.mp3"
        good.parent.mkdir(); good.write_bytes(b"audio")
        outside = Path(self.tmp.name) / "outside.mp3"; outside.write_bytes(b"secret")
        os.symlink(outside, config.ASSET_ROOT / "escape.mp3")
        self.assertEqual(render_cards._music_bed({"music": "music/good.mp3"}), str(good.resolve()))
        for value in (str(outside), "../outside.mp3", "escape.mp3"):
            self.assertIsNone(render_cards._music_bed({"music": value}))

    def test_file_background_is_rejected_without_fetch(self):
        with mock.patch.object(stream_proxy, "_fetch") as fetch:
            self.assertIsNone(render_cards._bg_image("file:///etc/hostname"))
            fetch.assert_not_called()

    def test_corrupt_background_leaves_no_cache_or_partial(self):
        response = mock.Mock(headers={})
        response.read = mock.Mock(side_effect=[b"not an image", b""])
        response.close = mock.Mock()
        with mock.patch.object(stream_proxy, "_fetch", return_value=response):
            self.assertIsNone(render_cards._bg_image("https://example.com/a.jpg"))
        cache = config.ASSET_ROOT / render_cards._BG_CACHE
        self.assertEqual(list(cache.iterdir()), [])
        response.close.assert_called_once()

    def test_oversized_background_leaves_no_cache_or_partial(self):
        response = mock.Mock(headers={"Content-Length": "5"})
        response.read = mock.Mock(side_effect=[b"12345", b""])
        response.close = mock.Mock()
        old_limit = render_cards._BG_MAX
        render_cards._BG_MAX = 4
        self.addCleanup(setattr, render_cards, "_BG_MAX", old_limit)
        with mock.patch.object(stream_proxy, "_fetch", return_value=response):
            self.assertIsNone(render_cards._bg_image("https://example.com/large.jpg"))
        cache = config.ASSET_ROOT / render_cards._BG_CACHE
        self.assertEqual(list(cache.iterdir()), [])
        response.close.assert_called_once()

    def test_distinct_full_urls_have_distinct_cache_entries(self):
        buf = io.BytesIO(); Image.new("RGB", (2, 2), "red").save(buf, format="PNG")
        png = buf.getvalue()
        def response(*args):
            r = mock.Mock(headers={})
            r.read = mock.Mock(side_effect=[png, b""])
            r.close = mock.Mock()
            return r
        old_w, old_h = render_cards.W, render_cards.H
        render_cards.W = render_cards.H = 2
        self.addCleanup(setattr, render_cards, "W", old_w)
        self.addCleanup(setattr, render_cards, "H", old_h)
        with mock.patch.object(stream_proxy, "_fetch", side_effect=response):
            self.assertIsNotNone(render_cards._bg_image("https://example.com/a.jpg?x=1"))
            self.assertIsNotNone(render_cards._bg_image("https://example.com/a.jpg?x=2"))
        cache = config.ASSET_ROOT / render_cards._BG_CACHE
        self.assertEqual(len(list(cache.glob("*.img"))), 2)

    def test_cache_eviction_by_age_and_size(self):
        cache = config.ASSET_ROOT / render_cards._BG_CACHE; cache.mkdir(parents=True)
        old = cache / "old.img"; old.write_bytes(b"x" * 5)
        os.utime(old, (time.time() - 40 * 86400,) * 2)
        newer = cache / "new.img"; newer.write_bytes(b"x" * 10)
        self.assertEqual(render_cards.prune_bg_cache(max_age_days=30, max_bytes=5), 2)
        self.assertEqual(list(cache.iterdir()), [])

    def test_drawtext_escapes_brand_and_ffmpeg_accepts_filter(self):
        font = brandslam.font_pool()[0]
        punctuated = Path(self.tmp.name) / "font;a[b]:c'd%e.ttf"
        shutil.copy2(font, punctuated)
        brand = "a;b[c]:d'e%f"
        chain = produce._drawtext_chain(brand, None, punctuated, 3.0)[0]
        self.assertNotIn(brand, chain)
        textfile = produce._brand_textfile(brand)
        self.assertEqual(textfile.read_text(encoding="utf-8"), brand)
        if shutil.which("ffmpeg"):
            run = subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                 "color=size=1920x1080:duration=0.1", "-vf", chain,
                 "-frames:v", "1", "-f", "null", "-"],
                capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_repeated_production_never_overwrites_prior_render(self):
        originals = (config.DB_PATH, config.VIDEO_DIR, config.OUTPUT_DIR)
        config.DB_PATH = str(Path(self.tmp.name) / "produce.db")
        config.VIDEO_DIR = Path(self.tmp.name) / "source"
        config.OUTPUT_DIR = Path(self.tmp.name) / "output"
        config.VIDEO_DIR.mkdir(); config.OUTPUT_DIR.mkdir()
        self.addCleanup(setattr, config, "DB_PATH", originals[0])
        self.addCleanup(setattr, config, "VIDEO_DIR", originals[1])
        self.addCleanup(setattr, config, "OUTPUT_DIR", originals[2])
        source = config.VIDEO_DIR / "same-name.mp4"; source.write_bytes(b"source")
        db.init_db()

        def cut(_src, dest, *args, **kwargs):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"rendered")

        patches = (
            mock.patch.object(produce, "duration_of", side_effect=lambda p: 100 if Path(p) == source else 5),
            mock.patch.object(produce, "scene_cuts", return_value=[]),
            mock.patch.object(produce, "plan_windows", return_value=[(1.0, 5.0)]),
            mock.patch.object(produce, "mean_volume", return_value=None),
            mock.patch.object(produce, "cut_clip", side_effect=cut),
            mock.patch.object(produce.brandslam, "roll", return_value=None),
            mock.patch.object(produce.brandslam, "static_face", return_value=None),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            first, _ = produce.produce_from_source(source, "ambient", __import__("random").Random(1),
                                                    [], [], ({}, {}))
            second, _ = produce.produce_from_source(source, "ambient", __import__("random").Random(1),
                                                     [], [], ({}, {}))
        self.assertNotEqual(first[0][0], second[0][0])
        self.assertTrue((config.OUTPUT_DIR / first[0][0]).is_file())
        self.assertTrue((config.OUTPUT_DIR / second[0][0]).is_file())
        with db.conn() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM playables WHERE source='produced'").fetchone()[0], 2)

    def test_equal_prefix_sources_never_share_an_output(self):
        originals = (config.DB_PATH, config.VIDEO_DIR, config.OUTPUT_DIR)
        config.DB_PATH = str(Path(self.tmp.name) / "prefix.db")
        config.VIDEO_DIR = Path(self.tmp.name) / "prefix-source"
        config.OUTPUT_DIR = Path(self.tmp.name) / "prefix-output"
        config.VIDEO_DIR.mkdir(); config.OUTPUT_DIR.mkdir()
        self.addCleanup(setattr, config, "DB_PATH", originals[0])
        self.addCleanup(setattr, config, "VIDEO_DIR", originals[1])
        self.addCleanup(setattr, config, "OUTPUT_DIR", originals[2])
        prefix = "same-prefix-" + "x" * 60
        sources = [config.VIDEO_DIR / (prefix + suffix + ".mp4")
                   for suffix in ("-a", "-b")]
        for source in sources:
            source.write_bytes(b"source")
        db.init_db()

        def cut(_src, dest, *args, **kwargs):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"rendered")

        with mock.patch.object(produce, "duration_of", side_effect=lambda p: 100 if Path(p) in sources else 5), \
                mock.patch.object(produce, "scene_cuts", return_value=[]), \
                mock.patch.object(produce, "plan_windows", return_value=[(1.0, 5.0)]), \
                mock.patch.object(produce, "mean_volume", return_value=None), \
                mock.patch.object(produce, "cut_clip", side_effect=cut), \
                mock.patch.object(produce.brandslam, "roll", return_value=None), \
                mock.patch.object(produce.brandslam, "static_face", return_value=None):
            made = [produce.produce_from_source(
                source, "ambient", __import__("random").Random(1), [], [], ({}, {}))[0][0]
                for source in sources]
        self.assertNotEqual(made[0][0], made[1][0])
        self.assertTrue(all((config.OUTPUT_DIR / item[0]).is_file() for item in made))

    def test_production_registration_failure_removes_render(self):
        originals = (config.DB_PATH, config.VIDEO_DIR, config.OUTPUT_DIR)
        config.DB_PATH = str(Path(self.tmp.name) / "registration.db")
        config.VIDEO_DIR = Path(self.tmp.name) / "registration-source"
        config.OUTPUT_DIR = Path(self.tmp.name) / "registration-output"
        config.VIDEO_DIR.mkdir(); config.OUTPUT_DIR.mkdir()
        self.addCleanup(setattr, config, "DB_PATH", originals[0])
        self.addCleanup(setattr, config, "VIDEO_DIR", originals[1])
        self.addCleanup(setattr, config, "OUTPUT_DIR", originals[2])
        source = config.VIDEO_DIR / "source.mp4"
        source.write_bytes(b"source")
        db.init_db()

        def cut(_src, dest, *args, **kwargs):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"unregistered")

        @contextmanager
        def failed_registration():
            class Connection:
                def execute(self, *_args, **_kwargs):
                    raise RuntimeError("forced registration failure")
            yield Connection()

        with mock.patch.object(produce, "duration_of", side_effect=lambda p: 100 if Path(p) == source else 5), \
                mock.patch.object(produce, "scene_cuts", return_value=[]), \
                mock.patch.object(produce, "plan_windows", return_value=[(1.0, 5.0)]), \
                mock.patch.object(produce, "mean_volume", return_value=None), \
                mock.patch.object(produce, "cut_clip", side_effect=cut), \
                mock.patch.object(produce.brandslam, "roll", return_value=None), \
                mock.patch.object(produce.brandslam, "static_face", return_value=None), \
                mock.patch.object(produce.db, "conn", failed_registration):
            with self.assertRaisesRegex(RuntimeError, "forced registration"):
                produce.produce_from_source(source, "ambient", __import__("random").Random(1),
                                            [], [], ({}, {}))
        self.assertEqual(list(config.OUTPUT_DIR.rglob("*.mp4")), [])

    def test_same_second_station_id_runs_do_not_collide(self):
        original_db, original_output = config.DB_PATH, config.OUTPUT_DIR
        config.DB_PATH = str(Path(self.tmp.name) / "station.db")
        config.OUTPUT_DIR = Path(self.tmp.name) / "station-output"
        config.OUTPUT_DIR.mkdir()
        self.addCleanup(setattr, config, "DB_PATH", original_db)
        self.addCleanup(setattr, config, "OUTPUT_DIR", original_output)
        db.init_db()

        def render(dest, *_args, **_kwargs):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"rendered")

        with mock.patch.object(station_ids, "_sources", return_value=([], [])), \
                mock.patch.object(station_ids, "_render_still", side_effect=render), \
                mock.patch.object(station_ids.brandslam, "font_pool", return_value=[]), \
                mock.patch.object(station_ids.brandslam, "roll", return_value=None), \
                mock.patch.object(station_ids.brandslam, "static_face", return_value=None), \
                mock.patch.object(station_ids.time, "time", return_value=1234567890):
            first = station_ids.generate(count=1, seed=1)
            second = station_ids.generate(count=1, seed=1)
        self.assertNotEqual(first[0][0], second[0][0])
        self.assertTrue((config.OUTPUT_DIR / first[0][0].removeprefix("bumpers/")).is_file())
        self.assertTrue((config.OUTPUT_DIR / second[0][0].removeprefix("bumpers/")).is_file())

    def test_station_registration_failure_removes_render(self):
        original_db, original_output = config.DB_PATH, config.OUTPUT_DIR
        config.DB_PATH = str(Path(self.tmp.name) / "station-failure.db")
        config.OUTPUT_DIR = Path(self.tmp.name) / "station-failure-output"
        config.OUTPUT_DIR.mkdir()
        self.addCleanup(setattr, config, "DB_PATH", original_db)
        self.addCleanup(setattr, config, "OUTPUT_DIR", original_output)
        db.init_db()

        def render(dest, *_args, **_kwargs):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"unregistered")

        @contextmanager
        def failed_registration():
            class Connection:
                def execute(self, *_args, **_kwargs):
                    raise RuntimeError("forced registration failure")
            yield Connection()

        with mock.patch.object(station_ids, "_sources", return_value=([], [])), \
                mock.patch.object(station_ids, "_render_still", side_effect=render), \
                mock.patch.object(station_ids.brandslam, "font_pool", return_value=[]), \
                mock.patch.object(station_ids.brandslam, "roll", return_value=None), \
                mock.patch.object(station_ids.brandslam, "static_face", return_value=None), \
                mock.patch.object(station_ids.db, "conn", failed_registration):
            made = station_ids.generate(count=1, seed=1)
        self.assertEqual(made, [])
        self.assertEqual(list(config.OUTPUT_DIR.rglob("*.mp4")), [])

    def test_failed_forced_card_refresh_preserves_previous_render(self):
        dest = config.ASSET_ROOT / render_cards.OUT_SUBDIR / "card_local.mp4"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"known-good-render")
        row = {"id": "card:local", "kind": "local_time", "payload": "{}",
               "duration": 1, "title": "Clock"}

        def fail(partial, *_args, **_kwargs):
            Path(partial).write_bytes(b"partial")
            raise RuntimeError("forced encoder failure")

        with mock.patch.dict(render_cards.ANIMATED_BUILDERS,
                             {"local_time": lambda *_args: (lambda _t: None)}), \
                mock.patch.object(render_cards, "_encode_frames", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "forced encoder"):
                render_cards.render_one(row, "card-font", "brand-font", "TV",
                                        force=True)
        self.assertEqual(dest.read_bytes(), b"known-good-render")
        self.assertEqual(list(dest.parent.glob(".*.part.mp4")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
