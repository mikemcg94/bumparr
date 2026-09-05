import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from bumparr import render_cards, station_ids


class _Input:
    def write(self, _data):
        return None

    def close(self):
        return None


class _Process:
    def __init__(self, timeout=True, returncode=0):
        self.stdin = _Input()
        self.returncode = returncode
        self.killed = False
        self.calls = 0
        self.timeout = timeout

    def communicate(self, timeout=None):
        self.calls += 1
        if self.timeout and self.calls == 1:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return None, b"bounded diagnostic tail"

    def kill(self):
        self.killed = True
        self.returncode = -9


class ProcessLifecycle(unittest.TestCase):
    def test_render_frame_timeout_kills_reaps_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "partial.mp4"
            dest.write_bytes(b"partial")
            process = _Process()
            frame = Image.new("RGB", (1, 1), "black")
            with mock.patch.object(subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "ffmpeg timed out"):
                    render_cards._encode_frames(dest, lambda _t: frame, 0, fps=1)
            self.assertTrue(process.killed)
            self.assertEqual(process.calls, 2)
            self.assertFalse(dest.exists())

    def test_station_id_timeout_kills_reaps_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "partial.mp4"
            dest.write_bytes(b"partial")
            process = _Process()
            plate = Image.new("RGB", (1, 1), "black")
            with mock.patch.object(subprocess, "Popen", return_value=process), \
                    mock.patch.object(station_ids.brandslam, "draw", return_value=plate):
                with self.assertRaisesRegex(RuntimeError, "ffmpeg timed out"):
                    station_ids._render_still(dest, plate, "TV", None, 0)
            self.assertTrue(process.killed)
            self.assertEqual(process.calls, 2)
            self.assertFalse(dest.exists())

    def test_successful_render_drains_stderr_with_communicate(self):
        process = _Process(timeout=False)
        frame = Image.new("RGB", (1, 1), "black")
        with mock.patch.object(subprocess, "Popen", return_value=process):
            render_cards._encode_frames("unused.mp4", lambda _t: frame, 0, fps=1)
        self.assertEqual(process.calls, 1)
        self.assertFalse(process.killed)

    def test_nonzero_render_removes_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = Image.new("RGB", (1, 1), "black")
            for module, call in (
                    (render_cards, lambda dest: render_cards._encode_frames(
                        dest, lambda _t: frame, 0, fps=1)),
                    (station_ids, lambda dest: station_ids._render_still(
                        dest, frame, "TV", None, 0))):
                dest = Path(tmp) / (module.__name__.split(".")[-1] + ".mp4")
                dest.write_bytes(b"partial")
                process = _Process(timeout=False, returncode=1)
                with mock.patch.object(subprocess, "Popen", return_value=process):
                    with self.assertRaises(RuntimeError):
                        call(dest)
                self.assertFalse(dest.exists())


class FramePipe(unittest.TestCase):
    """The shared helper owns the kill/reap/unlink contract both encoders rely on."""

    def test_timeout_kills_reaps_and_removes_partial(self):
        from bumparr import ffmpeg_pipe
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "partial.mp4"
            dest.write_bytes(b"partial")
            process = _Process()
            with mock.patch.object(subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "ffmpeg timed out"):
                    ffmpeg_pipe.encode_frames(["ffmpeg"], [b"\x00"], dest, timeout=1)
            self.assertTrue(process.killed)
            self.assertEqual(process.calls, 2)
            self.assertFalse(dest.exists())

    def test_nonzero_exit_removes_partial_and_reports_tail(self):
        from bumparr import ffmpeg_pipe
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "partial.mp4"
            dest.write_bytes(b"partial")
            process = _Process(timeout=False, returncode=1)
            with mock.patch.object(subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "bounded diagnostic tail"):
                    ffmpeg_pipe.encode_frames(["ffmpeg"], [b"\x00"], dest, timeout=1)
            self.assertFalse(dest.exists())

    def test_success_drains_stderr_once_and_keeps_dest(self):
        from bumparr import ffmpeg_pipe
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.mp4"
            dest.write_bytes(b"encoded")
            process = _Process(timeout=False, returncode=0)
            with mock.patch.object(subprocess, "Popen", return_value=process):
                self.assertIsNone(ffmpeg_pipe.encode_frames(["ffmpeg"], [b"\x00"], dest, timeout=1))
            self.assertEqual(process.calls, 1)
            self.assertTrue(dest.exists())

    def test_frames_are_pulled_lazily(self):
        from bumparr import ffmpeg_pipe
        pulled = []

        def gen():
            for i in range(3):
                pulled.append(i)
                yield b"\x00"
        process = _Process(timeout=False, returncode=0)
        with mock.patch.object(subprocess, "Popen", return_value=process):
            ffmpeg_pipe.encode_frames(["ffmpeg"], gen(), "unused.mp4", timeout=1)
        self.assertEqual(pulled, [0, 1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
