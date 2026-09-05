import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bumparr import config, db, seed


class SeedQuality(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.originals = config.DB_PATH, config.ASSET_ROOT, config.OUTPUT_DIR
        config.DB_PATH = str(Path(self.tmp.name) / "seed.db")
        config.ASSET_ROOT = Path(self.tmp.name) / "assets"
        config.OUTPUT_DIR = config.ASSET_ROOT / "bumpers"
        config.ASSET_ROOT.mkdir(); config.OUTPUT_DIR.mkdir()
        for attr, value in zip(("DB_PATH", "ASSET_ROOT", "OUTPUT_DIR"), self.originals):
            self.addCleanup(setattr, config, attr, value)
        db.init_db()

    def test_only_new_files_are_probed_and_root_files_are_unsorted(self):
        old = config.ASSET_ROOT / "ambient" / "old.mp4"
        old.parent.mkdir(); old.write_bytes(b"old")
        root = config.ASSET_ROOT / "new.mp4"; root.write_bytes(b"new")
        with db.conn() as connection:
            connection.execute(
                "INSERT INTO playables (id,type,kind,uri,duration) VALUES (?,?,?,?,?)",
                ("vid:ambient/old.mp4", "video", "ambient", "ambient/old.mp4", 3))
        with mock.patch.object(seed, "_probe_duration", return_value=4) as probe:
            self.assertEqual(seed.seed_from_assets(), 1)
        probe.assert_called_once_with(root)
        with db.conn() as connection:
            row = connection.execute("SELECT kind FROM playables WHERE id='vid:new.mp4'").fetchone()
        self.assertEqual(row["kind"], "unsorted")

    def test_unreadable_new_file_is_skipped(self):
        clip = config.ASSET_ROOT / "bad.mp4"; clip.write_bytes(b"bad")
        with mock.patch.object(seed, "_probe_duration", return_value=None):
            self.assertEqual(seed.seed_from_assets(), 0)
        with db.conn() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM playables WHERE id='vid:bad.mp4'").fetchone())

    def test_missing_files_park_media_and_clear_card_render(self):
        with db.conn() as connection:
            connection.executemany(
                "INSERT INTO playables (id,type,kind,uri,duration,enabled,health) VALUES (?,?,?,?,?,?,?)",
                [("v", "video", "ambient", "gone.mp4", 3, 1, "ok"),
                 ("i", "image", "poster", "gone.jpg", 3, 1, "ok"),
                 ("c", "card", "trivia", "bumpers/cards/gone.mp4", 3, 0, "ok")])
        seed.seed_from_assets()
        with db.conn() as connection:
            rows = {r["id"]: dict(r) for r in connection.execute(
                "SELECT id,uri,enabled,health FROM playables")}
        self.assertEqual((rows["v"]["enabled"], rows["v"]["health"]), (0, "dead"))
        self.assertEqual((rows["i"]["enabled"], rows["i"]["health"]), (0, "dead"))
        self.assertIsNone(rows["c"]["uri"])
        self.assertEqual(rows["c"]["enabled"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
