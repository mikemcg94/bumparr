import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock

import bumparr.sources.fetch_queue as fq


class _FakeHTTP:
    def __init__(self, data):
        self._data = data
        self._pos = 0
        self.headers = {}

    def read(self, n=262144):
        if self._pos >= len(self._data):
            return b""
        out = self._data[self._pos:self._pos + n]
        self._pos += n
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FetchQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_root = fq.ROOT
        self._orig_gap = fq.GAP
        self._orig_max = fq.MAX_MB
        fq.ROOT = self.tmp
        fq.GAP = 0

    def tearDown(self):
        fq.ROOT = self._orig_root
        fq.GAP = self._orig_gap
        fq.MAX_MB = self._orig_max
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _files_under(self, root):
        out = []
        for dp, _, fns in os.walk(root):
            for fn in fns:
                out.append(os.path.join(dp, fn))
        return out

    def test_traversal_nm_stays_inside(self):
        for hostile, flat in (("../../evil.mp4", "evil.mp4"), ("a/b.mp4", "b.mp4")):
            cat = "mycat"
            outdir = os.path.join(self.tmp, cat)
            if os.path.isdir(outdir):
                shutil.rmtree(outdir)
            done = set()
            meta = {"files": [{"name": hostile, "size": 1234}]}
            payload = b"y" * 30000
            with mock.patch.object(fq, "gj", return_value=meta):
                with mock.patch("urllib.request.urlopen", return_value=_FakeHTTP(payload)):
                    ok = fq.fetch("testid", cat, done)
            self.assertTrue(ok)
            self.assertIn("testid", done)
            expect = os.path.join(outdir, "testid__" + flat)
            self.assertTrue(os.path.isfile(expect), expect)
            for p in self._files_under(self.tmp):
                self.assertTrue(
                    os.path.realpath(p).startswith(os.path.realpath(outdir) + os.sep),
                    p)
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.mp4")))
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "b.mp4")))

    def test_hostile_ident_stays_inside(self):
        cat = "mycat2"
        outdir = os.path.join(self.tmp, cat)
        done = set()
        meta = {"files": [{"name": "good.mp4", "size": 1234}]}
        payload = b"y" * 30000
        with mock.patch.object(fq, "gj", return_value=meta):
            with mock.patch("urllib.request.urlopen", return_value=_FakeHTTP(payload)):
                ok = fq.fetch("../../evilid", cat, done)
        self.assertTrue(ok)
        for p in self._files_under(self.tmp):
            self.assertTrue(
                os.path.realpath(p).startswith(os.path.realpath(outdir) + os.sep),
                p)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evilid")))

    def test_lying_size_aborts_with_no_dest(self):
        fq.MAX_MB = 1
        cat = "mycat3"
        outdir = os.path.join(self.tmp, cat)
        done = set()
        meta = {"files": [{"name": "good.mp4", "size": 1024}]}
        payload = b"z" * (2 * 1024 * 1024)
        with mock.patch.object(fq, "gj", return_value=meta):
            with mock.patch("urllib.request.urlopen", return_value=_FakeHTTP(payload)):
                ok = fq.fetch("testid", cat, done)
        self.assertFalse(ok)
        self.assertNotIn("testid", done)
        expect = os.path.join(outdir, "testid__good.mp4")
        self.assertFalse(os.path.exists(expect))
        self.assertEqual(self._files_under(outdir), [])

    def test_category_traversal_and_absolute_are_rejected_before_network(self):
        for category in ("../../outside", "/tmp/outside", "a/b"):
            with mock.patch.object(fq, "gj") as metadata:
                self.assertFalse(fq.fetch("item", category, set()))
                metadata.assert_not_called()

    def test_midstream_failure_preserves_existing_destination_and_cleans_partial(self):
        cat, ident, name = "mycat4", "item", "clip.mp4"
        outdir = os.path.join(self.tmp, cat)
        os.makedirs(outdir)
        dest = os.path.join(outdir, ident + "__" + name)
        old = b"existing"
        with open(dest, "wb") as fh:
            fh.write(old)

        class Failing(_FakeHTTP):
            def read(self, n=262144):
                if self._pos:
                    raise OSError("connection lost")
                return super().read(10)

        meta = {"files": [{"name": name, "size": 1234}]}
        with mock.patch.object(fq, "gj", return_value=meta), \
                mock.patch("urllib.request.urlopen", return_value=Failing(b"x" * 100)):
            self.assertFalse(fq.fetch(ident, cat, set()))
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), old)
        self.assertFalse(any(fn.endswith(".part") for fn in os.listdir(outdir)))

    def test_archive_url_quotes_identifier_and_name_slashes(self):
        seen = []
        def opening(req, timeout):
            seen.append(req.full_url)
            return _FakeHTTP(b"x" * 30000)
        meta = {"files": [{"name": "a/b clip.mp4", "size": 1234}]}
        with mock.patch.object(fq, "gj", return_value=meta), \
                mock.patch("urllib.request.urlopen", side_effect=opening):
            self.assertTrue(fq.fetch("id/with space", "safe", set()))
        self.assertIn("id%2Fwith%20space/a%2Fb%20clip.mp4", seen[0])

    def test_malformed_metadata_entries_and_huge_names_are_bounded(self):
        huge = "x" * 10000 + ".mp4"
        meta = {"files": [None, "bad", {"name": 123},
                           {"name": huge, "size": 1234}]}
        with mock.patch.object(fq, "gj", return_value=meta), \
                mock.patch("urllib.request.urlopen",
                           return_value=_FakeHTTP(b"x" * 30000)):
            self.assertTrue(fq.fetch("item", "safe", set()))
        names = os.listdir(os.path.join(self.tmp, "safe"))
        self.assertEqual(len(names), 1)
        self.assertLessEqual(len(names[0].encode("utf-8")), 180)

    def test_combined_long_identifier_filename_and_partial_are_bounded(self):
        category = "long"
        outdir = os.path.join(self.tmp, category)
        seen_components = []

        class InspectingHTTP(_FakeHTTP):
            def read(self, n=262144):
                seen_components.extend(os.listdir(outdir))
                return super().read(n)

        ident = "i" * 220
        name = "n" * 220 + ".mp4"
        meta = {"files": [{"name": name, "size": 1234}]}
        with mock.patch.object(fq, "gj", return_value=meta), \
                mock.patch("urllib.request.urlopen",
                           return_value=InspectingHTTP(b"x" * 30000)):
            self.assertTrue(fq.fetch(ident, category, set()))
        final_names = os.listdir(outdir)
        self.assertEqual(len(final_names), 1)
        self.assertLessEqual(len(final_names[0].encode("utf-8")), 180)
        self.assertTrue(final_names[0].endswith(".mp4"))
        self.assertTrue(any(name.startswith(".bumparr-fetch-")
                            and name.endswith(".part") for name in seen_components))
        self.assertTrue(all(len(name.encode("utf-8")) <= 255
                            for name in seen_components))


if __name__ == "__main__":
    unittest.main(verbosity=2)
