import datetime
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from bumparr import dayparts
from bumparr.station import guide

TZ = datetime.timezone(datetime.timedelta(hours=-4))
NOW = datetime.datetime(2026, 9, 5, 9, 30, tzinfo=TZ)
PARTS = "dayparts:\n  morning:\n    hours: \"06:00-10:00\"\n    description: \"Weather.\"\n"


class Guide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        p = Path(self.tmp.name) / "d.yaml"; p.write_text(PARTS, encoding="utf-8")
        self.parts = dayparts.load_dayparts(p)
        patcher = mock.patch.object(dayparts, "load_dayparts", return_value=self.parts)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_two_channels_and_contiguous_programmes(self):
        root = ET.fromstring(guide.xmltv(NOW, "TV"))
        self.assertEqual([c.get("id") for c in root.findall("channel")], ["bumparr.live", "bumparr.standby"])
        self.assertEqual(root.find("channel/display-name").text, "TV")
        live = [p for p in root.findall("programme") if p.get("channel") == "bumparr.live"]
        self.assertEqual(live[0].get("start"), "20260905033000 -0400")
        self.assertEqual(live[-1].get("stop"), "20260906093000 -0400")
        for a, b in zip(live, live[1:]):
            self.assertEqual(a.get("stop"), b.get("start"))
        morning = [p for p in live if p.find("title").text == "TV — morning"]
        self.assertTrue(morning); self.assertEqual(morning[0].find("desc").text, "Weather.")
        standby = [p for p in root.findall("programme") if p.get("channel") == "bumparr.standby"]
        self.assertTrue(standby)
        self.assertTrue(all(p.find("title").text == "TV — Please stand by" for p in standby))
        self.assertLessEqual(standby[0].get("start"), "20260905033000 -0400")
        self.assertGreaterEqual(standby[-1].get("stop"), "20260906093000 -0400")


if __name__ == "__main__":
    unittest.main()
