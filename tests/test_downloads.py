"""Atomic/bounded direct-download and YouTube-capture regressions."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bumparr import ingest


class Response:
    def __init__(self, body, content_type="video/mp4", declared=None, fail=False):
        self.body, self.pos, self.fail = body, 0, fail
        self.headers = {"Content-Type": content_type}
        if declared is not None:
            self.headers["Content-Length"] = str(declared)

    def read(self, amount):
        if self.fail and self.pos:
            raise OSError("connection lost")
        chunk = self.body[self.pos:self.pos + amount]
        self.pos += len(chunk)
        return chunk

    def __enter__(self): return self
    def __exit__(self, *args): return False


class Downloads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.asset = ingest.ASSET
        self.cap = ingest.MAX_DOWNLOAD_MB
        ingest.ASSET = Path(self.tmp.name)
        ingest.MAX_DOWNLOAD_MB = 1
        self.addCleanup(setattr, ingest, "ASSET", self.asset)
        self.addCleanup(setattr, ingest, "MAX_DOWNLOAD_MB", self.cap)

    def _dest(self):
        dest = Path(self.tmp.name) / "safe" / "clip.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest

    def test_actual_oversize_and_midstream_failure_preserve_destination(self):
        for response in (Response(b"x" * (1024 * 1024 + 1)),
                         Response(b"x" * 100, fail=True)):
            dest = self._dest()
            dest.write_bytes(b"existing media")
            with mock.patch("urllib.request.urlopen", return_value=response), \
                    mock.patch.object(ingest, "_video_aspect", return_value=1.7):
                self.assertEqual(ingest._download_video(
                    "https://example.com/clip.mp4", "safe", title="clip.mp4",
                    reseed=False), "download failed")
            self.assertEqual(dest.read_bytes(), b"existing media")
            self.assertFalse(list(dest.parent.glob(".*.part")))

    def test_declared_oversize_and_html_are_rejected(self):
        for response in (Response(b"x", declared=2 * 1024 * 1024),
                         Response(b"<html>", content_type="text/html")):
            with mock.patch("urllib.request.urlopen", return_value=response):
                self.assertEqual(ingest._download_video(
                    "https://example.com/clip.mp4", "safe", title="clip.mp4",
                    reseed=False), "download failed")
            self.assertFalse(list((Path(self.tmp.name) / "safe").glob("*.part")))

    def test_invalid_category_is_rejected_before_network(self):
        with mock.patch("urllib.request.urlopen") as opening:
            self.assertIn("invalid category", ingest._download_video(
                "https://example.com/clip.mp4", "../outside", reseed=False))
            opening.assert_not_called()

    def test_capture_timeout_and_small_result_preserve_existing(self):
        dest = Path(self.tmp.name) / "windows" / "cam.mp4"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"existing" * 10000)
        resolved = subprocess.CompletedProcess([], 0, "https://example.com/live\n", "")

        for small in (False, True):
            def run(cmd, small=small, **kwargs):
                if cmd[0].endswith("yt-dlp"):
                    return resolved
                if small:
                    Path(cmd[-1]).write_bytes(b"tiny")
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                raise subprocess.TimeoutExpired(cmd, 1)
            with mock.patch.object(ingest, "_which", return_value="yt-dlp"), \
                    mock.patch.object(ingest.subprocess, "run", side_effect=run):
                self.assertEqual(ingest._capture_youtube(
                    "https://youtube.com/watch?v=x", category="windows", slug="cam"),
                    "capture failed")
            self.assertEqual(dest.read_bytes(), b"existing" * 10000)
            self.assertFalse(list(dest.parent.glob(".*.part.mp4")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
