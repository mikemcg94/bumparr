"""Regression tests for the shared media-path resolver (M1).

A crafted registry uri must never resolve outside the media trees: the
review's repro showed `Path(ASSET_ROOT)/uri` with an un-normalized uri
deleting files elsewhere on the filesystem.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bumparr import config, db, paths, prune


class ResolveMedia(unittest.TestCase):
    """paths.resolve_media containment across both media trees."""

    def setUp(self):
        """Point both media roots at fresh temp dirs for each test."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_assets = config.ASSET_ROOT
        self._orig_output = config.OUTPUT_DIR
        self.addCleanup(setattr, config, "ASSET_ROOT", self._orig_assets)
        self.addCleanup(setattr, config, "OUTPUT_DIR", self._orig_output)
        config.ASSET_ROOT = Path(self.tmp.name) / "assets"
        config.OUTPUT_DIR = Path(self.tmp.name) / "assets" / "bumpers"
        config.ASSET_ROOT.mkdir(parents=True)
        config.OUTPUT_DIR.mkdir(parents=True)

    def test_traversal_uris_resolve_to_none(self):
        """Parent-directory escapes never resolve to a filesystem path."""
        for uri in ("../evil.mp4", "../../etc/x", "a/../../b",
                    "bumpers/../../evil.mp4"):
            self.assertIsNone(paths.resolve_media(uri), uri)

    def test_absolute_uri_resolves_to_none(self):
        """An absolute uri is not inside either media tree."""
        self.assertIsNone(paths.resolve_media("/etc/hostname"))

    def test_remote_and_empty_uris_resolve_to_none(self):
        """Streams and missing uris have no local file (unchanged semantics)."""
        for uri in (None, "", "http://example.com/x.mp4",
                    "https://example.com/x.mp4", "HTTPS://EXAMPLE.COM/X.MP4"):
            self.assertIsNone(paths.resolve_media(uri), repr(uri))

    def test_bumpers_prefix_resolves_under_output_dir(self):
        """A bumpers/ uri stays contained in OUTPUT_DIR."""
        p = paths.resolve_media("bumpers/clip.mp4")
        self.assertIsNotNone(p)
        self.assertEqual(p, config.OUTPUT_DIR.resolve() / "clip.mp4")

    def test_plain_uri_resolves_under_asset_root(self):
        """An ordinary uri stays contained in ASSET_ROOT."""
        p = paths.resolve_media("ambient/clip.mp4")
        self.assertIsNotNone(p)
        self.assertEqual(p, config.ASSET_ROOT.resolve() / "ambient" / "clip.mp4")

    def test_symlink_escape_resolves_to_none(self):
        """A symlink inside the tree pointing outside does not resolve."""
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        os.symlink(str(outside), config.ASSET_ROOT / "link")
        self.assertIsNone(paths.resolve_media("link/secret.txt"))

    def test_contained_symlink_resolves_to_link_not_target(self):
        target = config.ASSET_ROOT / "ambient" / "target.mp4"
        target.parent.mkdir()
        target.write_bytes(b"target")
        link = target.parent / "alias.mp4"
        link.symlink_to(target)
        self.assertEqual(paths.resolve_media("ambient/alias.mp4"), link.absolute())


class ResolveKindDir(unittest.TestCase):
    """paths.resolve_kind_dir gates category-directory removal."""

    def setUp(self):
        """Point both media roots at fresh temp dirs for each test."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_assets = config.ASSET_ROOT
        self._orig_output = config.OUTPUT_DIR
        self.addCleanup(setattr, config, "ASSET_ROOT", self._orig_assets)
        self.addCleanup(setattr, config, "OUTPUT_DIR", self._orig_output)
        config.ASSET_ROOT = Path(self.tmp.name) / "assets"
        config.OUTPUT_DIR = Path(self.tmp.name) / "assets" / "bumpers"
        config.ASSET_ROOT.mkdir(parents=True)
        config.OUTPUT_DIR.mkdir(parents=True)

    def test_valid_kind_resolves_inside_root(self):
        """An ordinary category resolves to its directory under the root."""
        d = paths.resolve_kind_dir(config.ASSET_ROOT, "ambient")
        self.assertEqual(d, config.ASSET_ROOT.resolve() / "ambient")

    def test_empty_filename_fallback_can_be_explicit(self):
        self.assertEqual(paths.safe_filename("...", ""), "")

    def test_safe_filename_is_bounded_for_hostile_metadata(self):
        name = paths.safe_filename("a" * 10000 + ".mp4")
        self.assertLessEqual(len(name.encode("utf-8")), 180)
        self.assertTrue(name.endswith(".mp4"))

    def test_safe_filename_honors_explicit_complete_component_limit(self):
        name = paths.safe_filename("x" * 1000 + ".mp4", max_bytes=96)
        self.assertLessEqual(len(name.encode("utf-8")), 96)
        self.assertTrue(name.endswith(".mp4"))

    def test_maximum_length_entry_uses_short_quarantine_name(self):
        original = config.ASSET_ROOT / (("x" * 251) + ".mp4")
        original.write_bytes(b"media")
        staged = paths.stage_delete(original)
        self.assertLessEqual(len(staged.name.encode("utf-8")), 255)
        self.assertTrue(staged.is_file())
        self.assertFalse(original.exists())
        paths.restore_delete(original, staged)
        self.assertTrue(original.is_file())
        staged = paths.stage_delete(original)
        paths.finish_delete(staged)
        self.assertFalse(original.exists())
        self.assertFalse(staged.exists())

    def test_broken_symlink_can_be_staged_and_restored(self):
        original = config.ASSET_ROOT / "missing-alias.mp4"
        original.symlink_to(config.ASSET_ROOT / "missing-target.mp4")
        staged = paths.stage_delete(original)
        self.assertFalse(original.is_symlink())
        self.assertTrue(staged.is_symlink())
        paths.restore_delete(original, staged)
        self.assertTrue(original.is_symlink())
        self.assertFalse(staged.is_symlink())

    def test_traversal_absolute_and_empty_kinds_resolve_to_none(self):
        """Traversal, absolute, empty, and self kinds never resolve."""
        for kind in ("../outside", "..", "/etc", "", "  ", "."):
            self.assertIsNone(
                paths.resolve_kind_dir(config.ASSET_ROOT, kind), repr(kind))


class DeleteTraversal(unittest.TestCase):
    """End-to-end: a crafted row deletes its row but leaves the disk alone."""

    def setUp(self):
        """Isolate the DB and both media trees in temp dirs."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_db = config.DB_PATH
        self._orig_assets = config.ASSET_ROOT
        self._orig_output = config.OUTPUT_DIR
        self.addCleanup(setattr, config, "DB_PATH", self._orig_db)
        self.addCleanup(setattr, config, "ASSET_ROOT", self._orig_assets)
        self.addCleanup(setattr, config, "OUTPUT_DIR", self._orig_output)
        config.DB_PATH = str(Path(self.tmp.name) / "test.db")
        config.ASSET_ROOT = Path(self.tmp.name) / "assets"
        config.OUTPUT_DIR = Path(self.tmp.name) / "assets" / "bumpers"
        config.ASSET_ROOT.mkdir(parents=True)
        config.OUTPUT_DIR.mkdir(parents=True)
        db.init_db()

    def _row(self, rid):
        with db.conn() as c:
            return c.execute("SELECT * FROM playables WHERE id=?",
                             (rid,)).fetchone()

    def test_delete_bumper_with_traversal_uri_keeps_outside_file(self):
        """The review repro: a '..' row must not delete outside the trees."""
        from bumparr.app import delete_bumper
        sentinel = Path(self.tmp.name) / "sentinel.txt"
        sentinel.write_text("do not delete", encoding="utf-8")
        with db.conn() as c:
            c.execute(
                "INSERT INTO playables (id,type,kind,source,uri,duration,title) "
                "VALUES (?,?,?,?,?,?,?)",
                ("vid:evil", "video", "ambient", "local", "../sentinel.txt",
                 10.0, "evil"))
        res = delete_bumper("vid:evil")
        self.assertEqual(res["deleted"], "vid:evil")
        self.assertFalse(res["file_removed"])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not delete")
        self.assertIsNone(self._row("vid:evil"))

    def test_delete_bumper_with_plain_uri_removes_inside_file(self):
        """A legitimate in-tree file is still removed with its row."""
        from bumparr.app import delete_bumper
        clip = config.ASSET_ROOT / "ambient" / "clip.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"\x00" * 16)
        with db.conn() as c:
            c.execute(
                "INSERT INTO playables (id,type,kind,source,uri,duration,title) "
                "VALUES (?,?,?,?,?,?,?)",
                ("vid:good", "video", "ambient", "local",
                 "ambient/clip.mp4", 10.0, "good"))
        res = delete_bumper("vid:good")
        self.assertTrue(res["file_removed"])
        self.assertFalse(clip.exists())
        self.assertIsNone(self._row("vid:good"))

    def test_delete_bumper_removes_contained_symlink_not_shared_target(self):
        from bumparr.app import delete_bumper
        target = config.ASSET_ROOT / "ambient" / "target.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"shared media")
        link = target.parent / "alias.mp4"
        link.symlink_to(target)
        with db.conn() as c:
            c.executemany(
                "INSERT INTO playables (id,type,kind,source,uri,duration,title) "
                "VALUES (?,?,?,?,?,?,?)",
                [("vid:target", "video", "ambient", "local",
                  "ambient/target.mp4", 10.0, "target"),
                 ("vid:alias", "video", "ambient", "local",
                  "ambient/alias.mp4", 10.0, "alias")],
            )
        result = delete_bumper("vid:alias")
        self.assertTrue(result["file_removed"])
        self.assertFalse(link.exists())
        self.assertTrue(target.is_file())
        self.assertIsNotNone(self._row("vid:target"))
        self.assertIsNone(self._row("vid:alias"))

    def test_prune_removes_contained_symlink_not_shared_target(self):
        target = config.ASSET_ROOT / "ambient" / "prune-target.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"shared media")
        link = target.parent / "prune-alias.mp4"
        link.symlink_to(target)
        with db.conn() as c:
            c.executemany(
                "INSERT INTO playables (id,type,kind,source,uri,duration,title) "
                "VALUES (?,?,?,?,?,?,?)",
                [("vid:prune-target", "video", "ambient", "local",
                  "ambient/prune-target.mp4", 10.0, "target"),
                 ("vid:prune-alias", "video", "ambient", "local",
                  "ambient/prune-alias.mp4", 10.0, "alias")],
            )
        removed, cleanup_failed = prune._remove_registered([{
            "id": "vid:prune-alias",
            "uri": "ambient/prune-alias.mp4",
            "path": paths.resolve_media("ambient/prune-alias.mp4"),
        }])
        self.assertEqual(removed, ["ambient/prune-alias.mp4"])
        self.assertEqual(cleanup_failed, 0)
        self.assertFalse(link.exists())
        self.assertTrue(target.is_file())
        self.assertIsNotNone(self._row("vid:prune-target"))
        self.assertIsNone(self._row("vid:prune-alias"))

    def test_delete_kind_with_traversal_kind_keeps_outside_dir(self):
        """A traversal kind must not remove a directory outside the trees."""
        from bumparr.app import delete_kind
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        response = delete_kind("../outside")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(outside.is_dir())

    def _good_row_and_file(self, rid="vid:recover"):
        clip = config.ASSET_ROOT / "ambient" / (rid.replace(":", "_") + ".mp4")
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"good media")
        with db.conn() as c:
            c.execute(
                "INSERT INTO playables (id,type,kind,source,uri,duration,title) "
                "VALUES (?,?,?,?,?,?,?)",
                (rid, "video", "ambient", "local",
                 str(clip.relative_to(config.ASSET_ROOT)), 10.0, "good"))
        return clip

    def test_execute_failure_restores_staged_file_and_row(self):
        from bumparr import app
        clip = self._good_row_and_file()
        original_conn = app.db.conn

        @contextmanager
        def failing_conn():
            with original_conn() as connection:
                class Proxy:
                    def execute(self, sql, args=()):
                        if sql.startswith("DELETE"):
                            raise sqlite3.OperationalError("forced delete failure")
                        return connection.execute(sql, args)
                yield Proxy()

        with mock.patch.object(app.db, "conn", failing_conn):
            response = app.delete_bumper("vid:recover")
        self.assertEqual(response.status_code, 500)
        self.assertTrue(clip.is_file())
        self.assertIsNotNone(self._row("vid:recover"))

    def test_staging_failure_leaves_file_and_row(self):
        from bumparr import app
        clip = self._good_row_and_file()
        with mock.patch.object(app.paths, "stage_delete", side_effect=OSError("forced")):
            response = app.delete_bumper("vid:recover")
        self.assertEqual(response.status_code, 500)
        self.assertTrue(clip.is_file())
        self.assertIsNotNone(self._row("vid:recover"))

    def test_commit_failure_restores_staged_file_and_row(self):
        from bumparr import app
        clip = self._good_row_and_file()
        original_conn = app.db.conn

        @contextmanager
        def failing_commit():
            with original_conn() as connection:
                yield connection
                raise sqlite3.OperationalError("forced commit failure")

        with mock.patch.object(app.db, "conn", failing_commit):
            response = app.delete_bumper("vid:recover")
        self.assertEqual(response.status_code, 500)
        self.assertTrue(clip.is_file())
        self.assertIsNotNone(self._row("vid:recover"))

    def test_post_commit_cleanup_failure_is_reported_and_recoverable(self):
        from bumparr import app
        clip = self._good_row_and_file()
        with mock.patch.object(app.paths, "finish_delete", side_effect=OSError("forced")):
            result = app.delete_bumper("vid:recover")
        self.assertTrue(result["cleanup_failed"])
        self.assertFalse(result["file_removed"])
        self.assertIsNone(self._row("vid:recover"))
        self.assertFalse(clip.exists())
        self.assertEqual(len(list(clip.parent.glob(".bumparr-delete-*"))), 1)

    def test_bulk_commit_failure_restores_every_file_and_row(self):
        from bumparr import app
        clips = [self._good_row_and_file("vid:bulk-%d" % i) for i in range(2)]
        original_conn = app.db.conn

        @contextmanager
        def failing_commit():
            with original_conn() as connection:
                yield connection
                raise sqlite3.OperationalError("forced bulk commit failure")

        with mock.patch.object(app.db, "conn", failing_commit):
            response = app.delete_kind("ambient")
        self.assertEqual(response.status_code, 500)
        self.assertTrue(all(clip.is_file() for clip in clips))
        self.assertTrue(all(self._row("vid:bulk-%d" % i) is not None
                            for i in range(2)))

    def test_bulk_staging_failure_leaves_that_row_intact(self):
        from bumparr import app
        clip = self._good_row_and_file("vid:bulk-stage")
        with mock.patch.object(app.paths, "stage_delete", side_effect=OSError("forced")):
            result = app.delete_kind("ambient")
        self.assertEqual(result["removed"], 0)
        self.assertEqual(len(result["failed"]), 1)
        self.assertTrue(clip.is_file())
        self.assertIsNotNone(self._row("vid:bulk-stage"))

    def test_prune_commit_failure_restores_file_and_row(self):
        clip = self._good_row_and_file("vid:prune-recover")
        row = {"id": "vid:prune-recover", "uri": "ambient/vid_prune-recover.mp4",
               "path": clip}
        original_conn = prune.db.conn

        @contextmanager
        def failing_commit():
            with original_conn() as connection:
                yield connection
                raise sqlite3.OperationalError("forced prune commit failure")

        with mock.patch.object(prune.db, "conn", failing_commit):
            with self.assertRaises(sqlite3.OperationalError):
                prune._remove_registered([row])
        self.assertTrue(clip.is_file())
        self.assertIsNotNone(self._row("vid:prune-recover"))

    def test_prune_drop_category_rejects_traversal_before_inspection(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir(exist_ok=True)
        sentinel = outside / "keep.mp4"
        sentinel.write_bytes(b"keep")
        result = prune.drop_categories(["../outside"], apply=True)
        self.assertEqual(result.get("error"), "invalid category")
        self.assertEqual(sentinel.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main(verbosity=2)
