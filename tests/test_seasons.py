"""Regression tests for seasonal weighting (bumparr/seasons.py)."""
import datetime
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bumparr import seasons

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def spec(start, end, **kw):
    d = {"start": start, "end": end}
    d.update(kw)
    return d


class FactorCurves(unittest.TestCase):
    def test_in_window_returns_in_season(self):
        s = spec("10-01", "10-31", in_season=2.0)
        self.assertEqual(seasons.factor_for(s, datetime.date(2026, 10, 15)), 2.0)

    def test_off_weight_outside(self):
        s = spec("10-01", "10-31", in_season=2.0, off_weight=0.0)
        self.assertEqual(seasons.factor_for(s, datetime.date(2026, 4, 10)), 0.0)

    def test_lead_ramp(self):
        s = spec("10-01", "10-31", in_season=2.0, off_weight=0.0, lead_in=10)
        f = seasons.factor_for(s, datetime.date(2026, 9, 26))
        self.assertGreater(f, 0.0)
        self.assertLess(f, 2.0)
        self.assertAlmostEqual(f, 2.0 * (1.0 - 5 / 10.0))

    def test_tail_ramp(self):
        s = spec("10-01", "10-31", in_season=2.0, off_weight=0.0, tail=10)
        f = seasons.factor_for(s, datetime.date(2026, 11, 5))
        self.assertAlmostEqual(f, 2.0 * (1.0 - 5 / 10.0))

    def test_peak_boost_rises_and_falls_inside_window(self):
        s = spec("10-01", "10-31", peak="10-15", in_season=2.0,
                 peak_boost=1.5)
        at_peak = seasons.factor_for(s, datetime.date(2026, 10, 15))
        before = seasons.factor_for(s, datetime.date(2026, 10, 8))
        after = seasons.factor_for(s, datetime.date(2026, 10, 22))
        self.assertEqual(at_peak, 3.5)
        self.assertAlmostEqual(before, after)
        self.assertGreater(before, 2.0)
        self.assertLess(before, at_peak)

    def test_wraparound_window(self):
        s = spec("12-01", "01-15", in_season=2.0, off_weight=0.0)
        self.assertEqual(seasons.factor_for(s, datetime.date(2026, 12, 20)), 2.0)
        self.assertEqual(seasons.factor_for(s, datetime.date(2026, 1, 5)), 2.0)
        self.assertEqual(seasons.factor_for(s, datetime.date(2026, 6, 1)), 0.0)


class Feb29(unittest.TestCase):
    def test_leap_year_preserves_feb29(self):
        self.assertEqual(seasons._doy("02-29", 2024), 60)
        self.assertNotEqual(seasons._doy("02-29", 2024),
                            seasons._doy("02-28", 2024))

    def test_common_year_tolerates_feb29(self):
        self.assertEqual(seasons._doy("02-29", 2023), seasons._doy("02-28", 2023))
        self.assertEqual(seasons._doy("02-29", 2023), 59)

    def test_feb29_adjacent_window_stays_sane(self):
        s = spec("02-25", "03-05", in_season=2.0, off_weight=0.0)
        self.assertEqual(seasons.factor_for(s, datetime.date(2024, 2, 29)), 2.0)
        self.assertEqual(seasons.factor_for(s, datetime.date(2023, 2, 28)), 2.0)
        self.assertEqual(seasons.factor_for(s, datetime.date(2023, 7, 4)), 0.0)


class RestoreBaseWeights(unittest.TestCase):
    def test_heals_mutated_row(self):
        # DB_PATH is read at import time, so the DB-touching check runs in a
        # subprocess with a temp DB rather than in-process.
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, DB_PATH=os.path.join(tmp, "t.db"),
                       PYTHONPATH=REPO)
            probe = os.path.join(tmp, "probe_restore.py")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write(
                    "from bumparr import db, seasons\n"
                    "db.init_db()\n"
                    "with db.conn() as c:\n"
                    "    c.execute(\n"
                    "        \"INSERT INTO playables (id,type,kind,source,uri,duration,\"\n"
                    "        \"title,payload,tags,weight,enabled,health,created_at) VALUES \"\n"
                    "        \"('x','video','k','s','f.mp4',5,'T',\"\n"
                    "        \"'{\\\"base_weight\\\": 1.5}','',0.5,1,'ok',0)\")\n"
                    "n = seasons.restore_base_weights()\n"
                    "with db.conn() as c:\n"
                    "    got = c.execute(\n"
                    "        \"SELECT weight, payload FROM playables WHERE id='x'\"\n"
                    "    ).fetchone()\n"
                    "print('RESTORED', n, got['weight'], got['payload'])\n"
                )
            r = subprocess.run([sys.executable, probe], capture_output=True,
                               text=True, env=env, cwd=REPO, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("RESTORED 1 1.5", r.stdout)
        self.assertNotIn("base_weight", r.stdout)


if __name__ == "__main__":
    unittest.main()
