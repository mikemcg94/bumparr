"""Feed drawn frames to ffmpeg over stdin without ever leaving a wedged
process or a half-written file behind.

Station IDs and rendered cards both encode this way, and both once carried
their own copy of the same sequence. The copies had already drifted (one
waited 300s, the other 600s), and the sequence is subtle enough that a
drift is a bug: stdin has to be closed *before* waiting so ffmpeg can flush,
stderr has to be drained *while* waiting so a chatty run cannot deadlock the
pipe, and a timeout has to kill, reap, and unlink so neither a zombie nor a
truncated MP4 survives to be registered as a playable.
"""
import subprocess
from pathlib import Path


def encode_frames(cmd, frames, dest, *, timeout, tail=600):
    """Feed raw frames to an ffmpeg command over stdin and land `dest`, or land nothing.

    `cmd` is the full argv (ffmpeg reading rawvideo on pipe:0, writing `dest`).
    `frames` is an iterable of `bytes`, pulled lazily so the caller draws each
    frame only when ffmpeg is ready for it. Returns None on success. Raises
    RuntimeError, with `dest` already removed, when ffmpeg exits non-zero or
    exceeds `timeout` seconds after the last frame; the message ends with the
    last `tail` bytes of ffmpeg's stderr so the caller can log the cause.
    """
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    try:
        for frame in frames:
            proc.stdin.write(frame)
    except (BrokenPipeError, ValueError):
        pass                      # ffmpeg exited early; its stderr has the reason
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.stdin = None         # so communicate() does not try to close it again
    try:
        _, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        _, err = proc.communicate()
        _unlink(dest)
        raise RuntimeError("ffmpeg timed out: %s" % _tail(err, tail)) from exc
    if proc.returncode != 0:
        _unlink(dest)
        raise RuntimeError(_tail(err, tail) or "ffmpeg exited %s" % proc.returncode)


def _tail(err, n):
    return (err or b"").decode("utf-8", "ignore")[-n:]


def _unlink(dest):
    # The partial output is the hazard; a missing file is the goal, not an error.
    try:
        Path(dest).unlink()
    except OSError:
        pass
