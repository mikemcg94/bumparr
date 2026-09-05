import datetime
import tempfile
import unittest
from pathlib import Path

from bumparr import dayparts

PARTS = """
dayparts:
  overnight:
    hours: "22:00-06:00"
    description: "Windows and dead air."
    kinds: {window: 2.0, trivia: 0.4}
  morning:
    hours: "06:00-10:00"
    description: "Weather and the time."
    kinds: {weather: 3.0}
  evening:
    hours: "18:00-22:00"
    kinds: {trivia: 2.0}
"""

TZ = datetime.timezone(datetime.timedelta(hours=-4))


def at(h, m=0, day=5):
    return datetime.datetime(2026, 9, day, h, m, tzinfo=TZ)


class Parsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "dayparts.yaml"

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def test_windows_parse_including_wraparound(self):
        parts = dayparts.load_dayparts(self.write(PARTS))
        self.assertEqual(parts["overnight"]["start"], 22 * 60)
        self.assertEqual(parts["overnight"]["end"], 6 * 60)
        self.assertEqual(parts["morning"]["kinds"], {"weather": 3.0})
        self.assertEqual(parts["evening"]["description"], "")

    def test_missing_or_broken_file_means_no_dayparts(self):
        self.assertEqual(dayparts.load_dayparts(self.path / "nope.yaml"), {})
        self.assertEqual(dayparts.load_dayparts(self.write("dayparts: {a: {hours: 'x'}}")), {})

    def test_overlapping_windows_disable_the_file(self):
        text = PARTS + "  clash:\n    hours: \"09:00-11:00\"\n"
        self.assertEqual(dayparts.load_dayparts(self.write(text)), {})


class Selection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = Path(self.tmp.name) / "d.yaml"
        p.write_text(PARTS, encoding="utf-8")
        self.parts = dayparts.load_dayparts(p)

    def test_boundaries_are_half_open(self):
        self.assertEqual(dayparts.current(at(5, 59), self.parts)[0], "overnight")
        self.assertEqual(dayparts.current(at(6, 0), self.parts)[0], "morning")
        self.assertEqual(dayparts.current(at(23, 30), self.parts)[0], "overnight")
        self.assertEqual(dayparts.current(at(1, 0), self.parts)[0], "overnight")
        self.assertIsNone(dayparts.current(at(12, 0), self.parts))

    def test_factors_follow_the_current_window(self):
        self.assertEqual(dayparts.factors_now(at(7), self.parts), {"weather": 3.0})
        self.assertEqual(dayparts.factors_now(at(12), self.parts), {})
        self.assertEqual(dayparts.factors_now(at(3), self.parts), {"window": 2.0, "trivia": 0.4})

    def test_blocks_are_contiguous_and_fill_gaps_with_brand_hours(self):
        out = dayparts.blocks(at(9, 30), at(19, 0), "TV", self.parts)
        self.assertEqual(out[0][0], at(9, 30))
        self.assertEqual(out[-1][1], at(19, 0))
        for a, b in zip(out, out[1:]):
            self.assertEqual(a[1], b[0])
        self.assertEqual(out[0][2], "TV — morning")
        self.assertEqual(out[1], (at(10), at(11), "TV", ""))
        self.assertEqual(out[-1][2], "TV — evening")
        self.assertEqual(out[-1][0], at(18))

    def test_wrapped_window_block_runs_past_midnight(self):
        out = dayparts.blocks(at(21), at(7, day=6), "TV", self.parts)
        titles = [(s, e, t) for s, e, t, _ in out]
        self.assertIn((at(22), at(6, day=6), "TV — overnight"), titles)
        self.assertEqual(out[-1][2], "TV — morning")

    def test_no_parts_means_brand_hours_only(self):
        out = dayparts.blocks(at(9, 30), at(12), "TV", {})
        self.assertEqual([t for _, _, t, _ in out], ["TV", "TV", "TV"])
        self.assertEqual(out[0][1], at(10))


if __name__ == "__main__":
    unittest.main()
