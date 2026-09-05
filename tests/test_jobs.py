"""M6: background loops survive transient errors; stat failures read as unknown."""
import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bumparr import app as webapp
from bumparr import jobs


class RefreshOnce(unittest.TestCase):
    """One capture failure must not prevent the fetch-queue pass in the same cycle."""

    def test_failing_capture_does_not_block_fetch(self):
        """A stubbed failing capture still lets the fetch-queue pass run."""
        with mock.patch.object(jobs, "_run_capture", side_effect=RuntimeError("boom")), \
                mock.patch.object(jobs, "_run_fetch_queue") as fetch:
            asyncio.run(jobs._refresh_once())
            fetch.assert_called_once_with()

    def test_subsequent_refresh_succeeds(self):
        """After a failing cycle, a subsequent _refresh_once() succeeds."""
        with mock.patch.object(jobs, "_run_capture", side_effect=RuntimeError("boom")), \
                mock.patch.object(jobs, "_run_fetch_queue"):
            asyncio.run(jobs._refresh_once())
        with mock.patch.object(jobs, "_run_capture") as capture, \
                mock.patch.object(jobs, "_run_fetch_queue") as fetch:
            asyncio.run(jobs._refresh_once())
            capture.assert_called_once_with()
            fetch.assert_called_once_with()

    def test_initial_pass_captures_when_missing(self):
        """The initial pass captures when windows are missing, then fetches."""
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(jobs, "WINDOWS_DIR", Path(td)), \
                mock.patch.object(jobs, "_run_capture") as capture, \
                mock.patch.object(jobs, "_run_fetch_queue") as fetch:
            asyncio.run(jobs._refresh_once(initial=True))
            capture.assert_called_once_with()
            fetch.assert_called_once_with()

    def test_cancelled_error_reraised(self):
        """CancelledError is re-raised, and the fetch pass does not run after it."""
        with mock.patch.object(jobs, "_run_capture", side_effect=asyncio.CancelledError), \
                mock.patch.object(jobs, "_run_fetch_queue") as fetch:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(jobs._refresh_once())
            fetch.assert_not_called()


class DatedOnce(unittest.TestCase):
    """A failing rotation must not prevent the seasonal pass in the same cycle."""

    def test_failing_rotation_does_not_block_seasons(self):
        """A stubbed failing rotation still lets the seasons pass run."""
        with mock.patch.object(jobs, "_rotate_dated_cards", side_effect=RuntimeError("db locked")), \
                mock.patch.object(jobs, "_apply_seasons") as seasons:
            asyncio.run(jobs._dated_once())
            seasons.assert_called_once_with()

    def test_cancelled_error_reraised(self):
        """CancelledError from rotation propagates out of _dated_once."""
        with mock.patch.object(jobs, "_rotate_dated_cards", side_effect=asyncio.CancelledError), \
                mock.patch.object(jobs, "_apply_seasons") as seasons:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(jobs._dated_once())
            seasons.assert_not_called()


class NewestWindowAge(unittest.TestCase):
    """Stat failures mean 'unknown, capture' (None), never a crash."""

    def test_stat_error_returns_none(self):
        """An unreadable windows dir reads as unknown, not a raise."""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "win_000.mp4").write_bytes(b"x" * 100)
            with mock.patch.object(jobs, "WINDOWS_DIR", Path(td)), \
                    mock.patch.object(Path, "stat", side_effect=OSError("denied")):
                self.assertIsNone(jobs._newest_window_age())

    def test_missing_dir_returns_none(self):
        """No windows dir at all is unknown, not a raise."""
        with mock.patch.object(jobs, "WINDOWS_DIR", Path("/nonexistent-bumparr-windows")):
            self.assertIsNone(jobs._newest_window_age())


class ActionJobs(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.semaphore = webapp._JOB_SEMAPHORE
        webapp._JOB_SEMAPHORE = asyncio.Semaphore(2)
        with webapp._JOB_LOCK:
            webapp._JOBS.clear()
            webapp._JOB_TASKS.clear()

    async def asyncTearDown(self):
        tasks = list(webapp._JOB_TASKS.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        webapp._JOB_SEMAPHORE = self.semaphore

    async def test_finished_jobs_expire_but_working_jobs_are_retained(self):
        now = time.time()
        with webapp._JOB_LOCK:
            webapp._JOBS.update({
                "done": {"status": "done", "updated_at": now - webapp._JOB_TTL - 1},
                "working": {"status": "working", "updated_at": now - webapp._JOB_TTL - 1},
                "timed-out-active": {"status": "error", "worker_active": True,
                                     "updated_at": now - webapp._JOB_TTL - 1},
            })
        webapp._prune_jobs(now)
        self.assertNotIn("done", webapp._JOBS)
        self.assertIn("working", webapp._JOBS)
        self.assertIn("timed-out-active", webapp._JOBS)

    async def test_blocking_actions_never_exceed_concurrency_cap(self):
        lock = threading.Lock()
        active = maximum = 0

        def work():
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return "ok"

        results = [webapp._start_job("test", work) for _ in range(5)]
        self.assertTrue(all(result.get("job_id") for result in results))
        await asyncio.gather(*list(webapp._JOB_TASKS.values()))
        self.assertEqual(maximum, 2)
        self.assertTrue(all(job["status"] == "done" for job in webapp._JOBS.values()))

    async def test_timed_out_thread_keeps_slot_and_record_until_exit(self):
        webapp._JOB_SEMAPHORE = asyncio.Semaphore(1)
        started = threading.Event()
        release = threading.Event()
        second_ran = threading.Event()

        def slow():
            started.set()
            release.wait(timeout=2)
            return "late"

        first = webapp._start_job("slow", slow, deadline=0.02)
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        await asyncio.sleep(0.05)
        with webapp._JOB_LOCK:
            timed_out = dict(webapp._JOBS[first["job_id"]])
        self.assertEqual(timed_out["status"], "error")
        self.assertTrue(timed_out["worker_active"])

        second = webapp._start_job("second", lambda: second_ran.set() or "ok",
                                   deadline=1)
        await asyncio.sleep(0.05)
        self.assertFalse(second_ran.is_set())
        self.assertIn(first["job_id"], webapp._JOBS)
        release.set()
        await asyncio.gather(*list(webapp._JOB_TASKS.values()))
        self.assertTrue(second_ran.is_set())
        self.assertFalse(webapp._JOBS[first["job_id"]]["worker_active"])
        self.assertEqual(webapp._JOBS[second["job_id"]]["status"], "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
