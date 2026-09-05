"""drop_categories deletes unregistered files; the dry run must say so.

--apply stages every file in the category dir, registered or not. A preview that
lists only registered rows understates an irreversible action, which is worse
than no preview at all — and one that lists a registered file a second time as
an unregistered leftover overstates it, which is how an operator talks himself
out of trusting the preview at all.
"""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from bumparr import config, db, prune


class DropCategories(unittest.TestCase):
    SYMLINK_ROOT = False    # subclass flips this: ASSET_ROOT is itself a symlink

    def _asset_root(self):
        """The ASSET_ROOT this case runs under: a real dir, or a link to one."""
        real = Path(self.tmp.name) / "media"
        real.mkdir()
        if not self.SYMLINK_ROOT:
            return real
        link = Path(self.tmp.name) / "assets"
        link.symlink_to(real, target_is_directory=True)
        return link

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.originals = config.DB_PATH, config.ASSET_ROOT, config.OUTPUT_DIR
        config.DB_PATH = str(Path(self.tmp.name) / "prune.db")
        config.ASSET_ROOT = self._asset_root()
        config.OUTPUT_DIR = config.ASSET_ROOT / "bumpers"
        config.OUTPUT_DIR.mkdir()
        for attr, value in zip(("DB_PATH", "ASSET_ROOT", "OUTPUT_DIR"), self.originals):
            self.addCleanup(setattr, config, attr, value)
        db.init_db()
        self.junk = config.ASSET_ROOT / "junk"
        self.junk.mkdir()
        self.registered = self.junk / "known.mp4"
        self.registered.write_bytes(b"known")
        self.stray = self.junk / "operator_notes.txt"
        self.stray.write_bytes(b"mine")
        with db.conn() as c:
            c.execute("INSERT INTO playables (id,type,kind,uri,duration) VALUES (?,?,?,?,?)",
                      ("vid:junk/known.mp4", "video", "junk", "junk/known.mp4", 3))
            c.commit()

    def _run(self, apply):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = prune.drop_categories(["junk"], apply=apply)
        return out, buf.getvalue()

    def _lines(self, printed, needle):
        return [ln.strip() for ln in printed.splitlines() if needle in ln]

    def test_dry_run_lists_the_unregistered_file(self):
        """Exactly one line, and it carries the label — presence is not enough.

        A preview that labelled every file unregistered would satisfy a bare
        `assertIn`, and so would one that listed the stray twice.
        """
        _, printed = self._run(apply=False)
        stray = self._lines(printed, "operator_notes.txt")
        self.assertEqual(len(stray), 1, printed)
        self.assertTrue(stray[0].startswith("would remove unregistered "), printed)

    def test_dry_run_still_lists_registered_rows(self):
        """Once, by its registry uri — never also as an unregistered leftover."""
        _, printed = self._run(apply=False)
        self.assertEqual(self._lines(printed, "known.mp4"),
                         ["would remove junk/known.mp4"], printed)

    def test_dry_run_deletes_nothing(self):
        self._run(apply=False)
        self.assertTrue(self.registered.is_file())
        self.assertTrue(self.stray.is_file())

    def test_apply_removes_both_and_the_dir(self):
        out, _ = self._run(apply=True)
        self.assertEqual(out["removed"], 1)
        self.assertFalse(self.registered.exists())
        self.assertFalse(self.stray.exists())
        self.assertFalse(self.junk.exists())

    def test_apply_clears_the_registry_row(self):
        self._run(apply=True)
        with db.conn() as c:
            self.assertIsNone(c.execute(
                "SELECT 1 FROM playables WHERE id='vid:junk/known.mp4'").fetchone())


class DropCategoriesUnderSymlinkedRoot(DropCategories):
    """ASSET_ROOT itself a symlink — `/assets -> /mnt/user/media`, the NAS shape.

    Registry uris resolve lexically (through the link) while category dirs
    resolve past it, so the same file used to arrive under two names and the
    preview printed a registered file twice, the second time as a leftover an
    operator never put there. Every inherited case reruns under that root.
    """
    SYMLINK_ROOT = True

    def test_registered_and_stray_keep_their_own_labels(self):
        """The normalization must not swap the labels it exists to line up."""
        _, printed = self._run(apply=False)
        listed = self._lines(printed, "would remove ")
        self.assertIn("would remove junk/known.mp4", listed)
        self.assertEqual([ln for ln in listed if "unregistered" in ln],
                         [ln for ln in listed if "operator_notes.txt" in ln],
                         printed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
