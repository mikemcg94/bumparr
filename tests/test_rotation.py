"""Regression tests for the rotation scoring model (bumparr/rotation.py)."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bumparr import rotation


class NeverPlayed(unittest.TestCase):
    def test_never_played_outranks_just_played(self):
        now = time.time()
        ctx = {"kind_last": {}, "median_plays": 2, "season": {}}
        fresh = {"base": 1.0, "kind": "ambient", "last_played": 0, "play_count": 0}
        just = {"base": 1.0, "kind": "ambient", "last_played": now, "play_count": 0}
        self.assertGreater(rotation.score(fresh, ctx, now),
                           rotation.score(just, ctx, now))

    def test_never_played_recency_is_full(self):
        self.assertEqual(rotation.recency(0), 1.0)
        self.assertEqual(rotation.recency(None), 1.0)


class RecencyFloor(unittest.TestCase):
    def test_zero_age_returns_floor(self):
        now = time.time()
        self.assertEqual(rotation.recency(now, now), rotation.RECENCY_FLOOR)

    def test_recency_recovers_toward_one(self):
        now = time.time()
        just = rotation.recency(now, now)
        later = rotation.recency(now - 30 * 86400, now)
        self.assertLess(just, later)
        self.assertLessEqual(later, 1.0)


class FatigueClamp(unittest.TestCase):
    def test_massive_overplay_clamps_at_min(self):
        self.assertEqual(rotation.fatigue(10 ** 12, 1), rotation.FATIGUE_MIN)

    def test_unplayed_in_huge_pool_clamps_at_max(self):
        self.assertEqual(rotation.fatigue(0, 10 ** 12), rotation.FATIGUE_MAX)

    def test_bounds_hold_across_counts(self):
        for plays, med in [(0, 1), (3, 4), (45, 4), (1000, 4)]:
            f = rotation.fatigue(plays, med)
            self.assertGreaterEqual(f, rotation.FATIGUE_MIN)
            self.assertLessEqual(f, rotation.FATIGUE_MAX)


class ExplainConsistency(unittest.TestCase):
    def test_explain_keys_match_score(self):
        now = 1700000000.0
        item = {"base": 1.2, "kind": "ambient",
                "last_played": now - 3600, "play_count": 5}
        ctx = {"kind_last": {"ambient": now - 600}, "median_plays": 4,
               "season": {"ambient": 1.5}}
        ex = rotation.explain(item, ctx, now)
        self.assertEqual(set(ex), {"base", "season", "daypart", "recency",
                                  "affinity", "fatigue", "score"})
        self.assertAlmostEqual(ex["score"], round(rotation.score(item, ctx, now), 4))


class DaypartFactor(unittest.TestCase):
    def test_daypart_multiplies_and_defaults_to_one(self):
        from bumparr import rotation
        rows = [{"id": "a", "kind": "trivia", "weight": 1.0, "last_played": 0, "play_count": 0},
                {"id": "b", "kind": "window", "weight": 1.0, "last_played": 0, "play_count": 0}]
        plain, _ = rotation.weights_for(rows, None, 1000.0)
        boosted, ctx = rotation.weights_for(rows, None, 1000.0, {"trivia": 2.0})
        self.assertAlmostEqual(boosted[0], plain[0] * 2.0)
        self.assertAlmostEqual(boosted[1], plain[1])
        self.assertEqual(ctx["daypart"], {"trivia": 2.0})
        self.assertEqual(rotation.explain(rows[0], ctx, 1000.0)["daypart"], 2.0)
        self.assertEqual(rotation.explain(rows[1], ctx, 1000.0)["daypart"], 1.0)


if __name__ == "__main__":
    unittest.main()
